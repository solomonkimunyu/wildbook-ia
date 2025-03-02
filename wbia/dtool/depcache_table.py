# -*- coding: utf-8 -*-
"""
Module contining DependencyCacheTable

python -m dtool.depcache_control --exec-make_graph --show
python -m dtool.depcache_control --exec-make_graph --show --reduce

FIXME:
    RECTIFY: ismulti / ismodel need to be rectified. This indicate that this
        table recieves multiple inputs from at least one parent table.

    RECTIFY: Need to standardize parent rowids -vs- parent args.
        in one-to-one cases they are the same. In multi cases the rowids indicate
        a uuid and the args are the saved set of rowids that exist in the manifest.

    RECTIFY: is rowid_list row-major or column-major?
        I think currently rowid_list is row-major and rowid_listT is column-major
        but this may not be consistent.



"""
import logging
import re
import itertools as it
from os.path import join, exists

import networkx as nx
import utool as ut
import ubelt as ub

from wbia.dtool import sqlite3 as lite
from wbia.dtool.sql_control import SQLDatabaseController, compare_coldef_lists
from wbia.dtool.types import TYPE_TO_SQLTYPE

import time
import random
import tqdm


(print, rrr, profile) = ut.inject2(__name__, '[depcache_table]')
logger = logging.getLogger('wbia.dtool')


EXTERN_SUFFIX = '_extern_uri'

CONFIG_TABLE = 'config'
CONFIG_ROWID = 'config_rowid'
CONFIG_HASHID = 'config_hashid'
CONFIG_TABLENAME = 'config_tablename'  # tablename associated with config
CONFIG_STRID = 'config_strid'
CONFIG_DICT = 'config_dict'


# if ut.is_developer():
#     GRACE_PERIOD = 10
# else:
GRACE_PERIOD = ut.get_argval('--grace', type_=int, default=0)


class TableOutOfSyncError(Exception):
    """Raised when the code's table definition doesn't match the defition in the database"""

    def __init__(self, db, tablename, extended_msg):
        db_name = db._engine.url.database

        if getattr(db, 'schema', None):
            under_schema = f"under schema '{db.schema}' "
        else:
            # Not a table under a schema
            under_schema = ''
        msg = (
            f"database '{db_name}' "
            + under_schema
            + f"with table '{tablename}' does not match the code definition; "
            f"it's likely the database needs upgraded; {extended_msg}"
        )
        super().__init__(msg)


class ExternType(ub.NiceRepr):
    """
    Type to denote an external resource not saved in an SQL table
    """

    def __init__(self, read_func, write_func, extern_ext=None, extkey=None):
        self.write_func = write_func
        self.read_func = read_func
        self.extern_ext = extern_ext
        self.extkey = extkey

    def __nice__(self):
        ext = None
        ext = self.extkey if self.extkey else ext
        ext = self.extern_ext if self.extern_ext and ext else ext
        return '(%s, %s, %s)' % (
            ut.get_funcname(self.read_func),
            ut.get_funcname(self.write_func),
            ext,
        )


class ExternalStorageException(Exception):
    """Indicates a missing external file"""

    def __init__(self, *args, **kwargs):
        super(ExternalStorageException, self).__init__(*args, **kwargs)


def predrop_grace_period(tablename, seconds=None):
    """Hack that gives the user some time to abort deleting everything"""
    global GRACE_PERIOD
    warnmsg_fmt = ut.codeblock(
        """
        WARNING TABLE={tablename} IS MODIFIED

        About to reset (DROP) entire cache={tablename}.

        Generally this is OK and you shouldnt worry because depcache
        information should be recomputable.

        If you really dont want this to happen you have {seconds} seconds to
        kill this process before deletion occurs.
        """
    )
    if seconds is None:
        seconds = GRACE_PERIOD
        GRACE_PERIOD = max(0, GRACE_PERIOD // 2)
    warnmsg = warnmsg_fmt.format(tablename=tablename, seconds=seconds)
    # return ut.are_you_sure(warnmsg)
    return ut.grace_period(warnmsg, seconds)


def make_extern_io_funcs(table, cls):
    """Hack in read/write defaults for pickleable classes"""

    def _read_func(fpath, verbose=ut.VERBOSE):
        state_dict = ut.load_data(fpath, verbose=verbose)
        # FIXME: The constructor should not be called by default to conform to
        # pickle standards
        self = cls()
        self.__setstate__(state_dict)
        if hasattr(self, 'on_load'):
            self.on_load(table.depc)
        return self

    def _write_func(fpath, self, verbose=ut.VERBOSE):
        if hasattr(self, 'on_save'):
            self.on_save(table.depc, fpath)
        ut.save_data(fpath, self.__getstate__(), verbose=verbose, n=4)

    return _read_func, _write_func


@profile
def ensure_config_table(db):
    """SQL definition of configuration table."""
    config_addtable_kw = ut.odict(
        [
            ('tablename', CONFIG_TABLE),
            (
                'coldef_list',
                [
                    (CONFIG_ROWID, 'INTEGER PRIMARY KEY'),
                    (CONFIG_HASHID, 'TEXT'),
                    (CONFIG_TABLENAME, 'TEXT'),
                    (CONFIG_STRID, 'TEXT'),
                    (CONFIG_DICT, 'DICT'),
                ],
            ),
            ('docstr', 'table for algo configurations'),
            ('superkeys', [(CONFIG_HASHID,)]),
            ('dependson', []),
        ]
    )
    if not db.has_table(CONFIG_TABLE):
        db.add_table(**config_addtable_kw)
    else:
        current_state = db.get_table_autogen_dict(CONFIG_TABLE)
        new_state = config_addtable_kw
        results = compare_coldef_lists(
            current_state['coldef_list'], new_state['coldef_list']
        )
        if results:
            current_coldef, new_coldef = results
            raise TableOutOfSyncError(
                db,
                CONFIG_TABLE,
                f'Current schema: {current_coldef} Expected schema: {new_coldef}',
            )


@ut.reloadable_class
class _TableConfigHelper(object):
    """helper for configuration table"""

    def get_parent_rowids(self, rowid_list):
        """
        Args:
            rowid_list (list): native table rowids

        Returns:
            parent_rowids (list of tuples): tuples of parent rowids

        Example:
            >>> # TODO: Need a test that creates a table
            >>> # with two multi-dependencies and a two single dependencies
            >>> # Then add two items to this table, and for each item
            >>> # Find their parent inputs
        """
        parent_rowids = self.get_internal_columns(
            rowid_list, self.parent_id_colnames, unpack_scalars=True, keepwrap=True
        )
        return parent_rowids

    def get_parent_rowargs(self, rowid_list):
        """
        Args:
            rowid_list (list): native table rowids

        Returns:
            parent_rowids (list of tuples): tuples of parent rowids

        Example:
            >>> # TODO: Need a test that creates a table
            >>> # with two multi-dependencies and a two single dependencies
            >>> # Then add two items to this table, and for each item
            >>> # Find their parent inputs
        """
        parent_rowids = self.get_parent_rowids(rowid_list)
        parent_ismulti = self.get_parent_col_attr('ismulti')
        if any(parent_ismulti):
            # If any of the parent columns are multi-indexes, then lookup the
            # mapping from the aggregated uuid to the expanded rowid set.
            parent_args = []
            model_uuids = self.get_model_uuid(rowid_list)
            for rowid, uuid, p_id_list in zip(rowid_list, model_uuids, parent_rowids):
                input_info = self.get_model_inputs(uuid)
                fixed_args = []
                for p_name, p_id, flag in zip(
                    self.parent_id_colnames, p_id_list, parent_ismulti
                ):
                    if flag:
                        new_p_id = input_info[p_name + '_model_input']
                        col_uuid = input_info[p_name + '_multi_id']
                        assert (
                            col_uuid == p_id
                        ), 'the model input has unexpectedly changed'
                        fixed_args.append(new_p_id)
                    else:
                        fixed_args.append(p_id)
                parent_args.append(fixed_args)
        else:
            parent_args = parent_rowids
        return parent_args

    def get_row_parent_rowid_map(self, rowid_list):
        """
        >>> from wbia.dtool.depcache_table import *  # NOQA

        parent_rowid_dict = depc.['feat'].get_row_parent_rowid_map(rowid_list)
        key = parent_rowid_dict.keys()[0]
        val = parent_rowid_dict.values()[0]
        """
        parent_rowids = self.get_parent_rowids(rowid_list)
        parent_rowid_dict = dict(
            zip(self.parent_id_tablenames, ut.list_transpose(parent_rowids))
        )
        return parent_rowid_dict

    def get_config_history(self, rowid_list, assume_unique=True):
        """
        Returns the list of config objects for all properties in the dependency
        history of this object. Multi-edges are handled. Set assume_unique to
        be false if there might be parents with different configs for the same
        table.

        >>> from wbia.dtool.depcache_table import *  # NOQA

        parent_rowid_dict = depc.['feat'].get_row_parent_rowid_map(rowid_list)
        key = parent_rowid_dict.keys()[0]
        val = parent_rowid_dict.values()[0]
        """
        if assume_unique:
            rowid_list = rowid_list[0:1]
        tbl_cfgids = self.get_row_cfgid(rowid_list)
        cfgid2_rowids = ut.group_items(rowid_list, tbl_cfgids)
        unique_cfgids = cfgid2_rowids.keys()
        unique_cfgids = ut.filter_Nones(unique_cfgids)
        if len(unique_cfgids) == 0:
            return None
        unique_configs = self.get_config_from_rowid(unique_cfgids)

        # parent_rowids = self.get_parent_rowids(rowid_list)
        parent_rowargs = self.get_parent_rowargs(rowid_list)

        ret_list = [unique_configs]
        depc = self.depc
        rowargsT = ut.listT(parent_rowargs)
        parent_ismulti = self.get_parent_col_attr('ismulti')
        for tblname, ismulti, ids in zip(
            self.parent_id_tablenames, parent_ismulti, rowargsT
        ):
            if tblname == depc.root:
                continue
            if ismulti:
                ids = ids[0]
            parent_tbl = depc[tblname]
            ancestor_configs = parent_tbl.get_config_history(ids)
            if ancestor_configs is not None:
                ret_list.extend(ancestor_configs)
        return ret_list

    def __remove_old_configs(self):
        """
        table = ibs.depc['pairwise_match']
        """
        # developing
        # c = self.db.get_table_as_pandas('config')
        # t = self.db.get_table_as_pandas(self.tablename)

        # config_rowids = self.db.get_all_rowids(CONFIG_TABLE)
        # cfgdict_list = self.db.get(
        #     CONFIG_TABLE, colnames=(CONFIG_DICT,), id_iter=config_rowids,
        #     id_colname=CONFIG_ROWID)
        # bad_rowids = []
        # for rowid, cfgdict in zip(config_rowids, cfgdict_list):
        #     if cfgdict['version'] < 7:
        #         bad_rowids.append(rowid)

        command = ut.codeblock(
            """
            SELECT rowid, {} from {}
            """
        ).format(CONFIG_DICT, CONFIG_TABLE)
        self.db.cur.execute(command)

        bad_rowids = []
        for rowid, cfgdict in self.db.cur.fetchall():
            # MAKE GENERAL CONDITION
            if cfgdict['version'] < 7:
                bad_rowids.append(rowid)

        in_str = '(' + ', '.join(map(str, bad_rowids)) + ')'
        command = ut.codeblock(
            """
            SELECT rowid from {tablename}
            WHERE config_rowid IN {bad_rowids}
            """
        ).format(tablename=self.tablename, bad_rowids=in_str)
        # logger.info(command)
        self.db.cur.execute(command)
        rowids = ut.flatten(self.db.cur.fetchall())
        self.delete_rows(rowids, dry=True, verbose=True, delete_extern=True)

    def get_ancestor_rowids(self, rowid_list, target_table):
        parent_rowids = self.get_parent_rowids(rowid_list)
        depc = self.depc
        for tblname, ids in zip(
            self.parent_id_tablenames, ut.list_transpose(parent_rowids)
        ):
            if tblname == target_table:
                return ids
            parent_tbl = depc[tblname]
            ancestor_ids = parent_tbl.get_ancestor_rowids(ids, target_table)
            if ancestor_ids is not None:
                return ancestor_ids
        return None  # Base case

    def get_row_cfgid(self, rowid_list):
        """
        >>> from wbia.dtool.depcache_table import *  # NOQA
        """
        config_rowids = self.get_internal_columns(rowid_list, (CONFIG_ROWID,))
        return config_rowids

    def get_row_configs(self, rowid_list):
        """
        Example:
            >>> # ENABLE_DOCTEST
            >>> # xdoctest: +REQUIRES(module:wbia)
            >>> from wbia.algo.hots.query_request import *  # NOQA
            >>> from wbia.dtool.example_depcache import *  # NOQA
            >>> depc = testdata_depc()
            >>> table = depc['chip']
            >>> rowid_list = depc.get_rowids('chip', [1, 2], config={})
            >>> configs = table.get_row_configs(rowid_list)
        """
        config_rowids = self.get_row_cfgid(rowid_list)
        # Only look up the configs that are needed
        unique_config_rowids, groupxs = ut.group_indices(config_rowids)
        unique_configs = self.get_config_from_rowid(unique_config_rowids)
        configs = ut.ungroup_unique(unique_configs, groupxs, maxval=len(rowid_list) - 1)
        return configs

    def get_row_cfghashid(self, rowid_list):
        config_rowids = self.get_row_cfgid(rowid_list)
        config_hashids = self.get_config_hashid(config_rowids)
        return config_hashids

    def get_row_cfgstr(self, rowid_list):
        config_rowids = self.get_row_cfgid(rowid_list)
        cfgstr_list = self.db.get(
            CONFIG_TABLE,
            colnames=(CONFIG_STRID,),
            id_iter=config_rowids,
            id_colname=CONFIG_ROWID,
        )
        return cfgstr_list

    def get_config_rowid(self, config=None, _debug=None):
        if isinstance(config, int):
            config_rowid = config
        else:
            config_rowid = self.add_config(config)
        return config_rowid

    def get_config_hashid(self, config_rowid_list):
        hashid_list = self.db.get(
            CONFIG_TABLE,
            colnames=(CONFIG_HASHID,),
            id_iter=config_rowid_list,
            id_colname=CONFIG_ROWID,
        )
        return hashid_list

    def get_config_rowid_from_hashid(self, config_hashid_list):
        config_rowid_list = self.db.get(
            CONFIG_TABLE,
            colnames=(CONFIG_ROWID,),
            id_iter=config_hashid_list,
            id_colname=CONFIG_HASHID,
        )
        return config_rowid_list

    def get_config_from_rowid(self, config_rowids):
        cfgdict_list = self.db.get(
            CONFIG_TABLE,
            colnames=(CONFIG_DICT,),
            id_iter=config_rowids,
            id_colname=CONFIG_ROWID,
        )
        return [
            None if dict_ is None else self.configclass(**dict_) for dict_ in cfgdict_list
        ]

    # @profile
    def add_config(self, config, _debug=None):
        try:
            # assume config is AlgoRequest or TableConfig
            config_strid = config.get_cfgstr()
        except AttributeError:
            config_strid = ut.to_json(config)
        config_hashid = ut.hashstr27(config_strid)
        logger.debug('config_strid = %r' % (config_strid,))
        logger.debug('config_hashid = %r' % (config_hashid,))
        get_rowid_from_superkey = self.get_config_rowid_from_hashid
        colnames = (CONFIG_HASHID, CONFIG_TABLENAME, CONFIG_STRID, CONFIG_DICT)
        if hasattr(config, 'config'):
            # Hack for requests
            config = config.config
        cfgdict = config.__getstate__()
        param_list = [(config_hashid, self.tablename, config_strid, cfgdict)]
        config_rowid_list = self.db.add_cleanly(
            CONFIG_TABLE, colnames, param_list, get_rowid_from_superkey
        )
        config_rowid = config_rowid_list[0]
        logger.debug('config_rowid_list = %r' % (config_rowid_list,))
        return config_rowid


@ut.reloadable_class
class _TableDebugHelper(object):
    """
    Contains printing and debug things
    """

    def print_sql_info(self):
        add_op = self.db._make_add_table_sqlstr(sep='\n    ', **self._get_addtable_kw())
        ut.cprint(add_op, 'sql')

    def print_internal_info(self, all_attrs=False):
        """
        CommandLine:
            python -m dtool.depcache_table --exec-print_internal_info

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> tablenames = ['labeler', 'vsone', 'neighbs', 'indexer']
            >>> for table in ut.take(depc, tablenames): # .tables:
            >>>     table.print_internal_info()
        """
        logger.info('----')
        logger.info(self)
        # Print the other inferred attrs
        logger.info(
            'self.parent_col_attrs = %s' % (ut.repr3(self.parent_col_attrs, nl=2),)
        )
        logger.info('self.data_col_attrs = %s' % (ut.repr3(self.data_col_attrs, nl=2),))
        # Print the inferred allcol attrs
        ut.cprint(
            'self.internal_col_attrs = %s'
            % (ut.repr3(self.internal_col_attrs, nl=1, sorted_=False)),
            'python',
        )
        add_table_kw = self._get_addtable_kw()
        logger.info('self.add_table_kw = %s' % (ut.repr2(add_table_kw, nl=2),))
        self.print_sql_info()
        if all_attrs:
            # Print all attributes
            for a in ut.get_instance_attrnames(self, with_properties=True, default=False):
                logger.info('  self.%s = %r' % (a, getattr(self, a)))

    def print_table(self):
        self.db.print_table_csv(self.tablename)
        # if self.ismulti:
        #    self.print_model_manifests()

    def print_info(self, with_colattrs=True, with_graphattrs=True):
        """debug function"""
        logger.info('TABLE ATTRIBUTES')
        logger.info('self.tablename = %r' % (self.tablename,))
        logger.info('self.rm_extern_on_delete = %r' % (self.rm_extern_on_delete,))
        logger.info('self.chunksize = %r' % (self.chunksize,))
        logger.info('self.fname = %r' % (self.fname,))
        logger.info('self.docstr = %r' % (self.docstr,))
        logger.info('self.data_colnames = %r' % (self.data_colnames,))
        logger.info('self.data_coltypes = %r' % (self.data_coltypes,))
        if with_graphattrs:
            logger.info('TABLE GRAPH ATTRIBUTES')
            logger.info('self.children = %r' % (self.children,))
            logger.info('self.parent = %r' % (self.parent,))
            logger.info('self.configclass = %r' % (self.configclass,))
            logger.info('self.requestclass = %r' % (self.requestclass,))
        if with_colattrs:
            nl = 1
            logger.info('TABEL COLUMN ATTRIBUTES')
            logger.info(
                'self.data_col_attrs = %s' % (ut.repr3(self.data_col_attrs, nl=nl),)
            )
            logger.info(
                'self.parent_col_attrs = %s' % (ut.repr3(self.parent_col_attrs, nl=nl),)
            )
            logger.info(
                'self.internal_data_col_attrs = %s'
                % (ut.repr3(self.internal_data_col_attrs, nl=nl),)
            )
            logger.info(
                'self.internal_parent_col_attrs = %s'
                % (ut.repr3(self.internal_parent_col_attrs, nl=nl),)
            )
            logger.info(
                'self.internal_col_attrs = %s'
                % (ut.repr3(self.internal_col_attrs, nl=nl),)
            )

    def print_schemadef(self):
        logger.info('\n'.join(self.db.get_table_autogen_str(self.tablename)))

    def print_configs(self):
        """
        CommandLine:
            python -m dtool.depcache_table --exec-print_configs

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache import testdata_depc
            >>> depc = testdata_depc()
            >>> table = depc['keypoint']
            >>> config = table.configclass()
            >>> rowids = table.get_rowids_from_root([1, 2], config=config)
            >>> config = table.configclass(adapt_shape=False)
            >>> rowids = table.get_rowids_from_root([1, 2], config=config)
            >>> table.print_configs()
            >>> table = depc['chip']
            >>> rowids = depc.get_rowids('spam', [1, 2])
            >>> table.print_configs()
        """
        text = self.db.get_table_csv(CONFIG_TABLE)
        logger.info(text)

    def print_csv(self, truncate=True):
        logger.info(self.db.get_table_csv(self.tablename, truncate=truncate))

    def print_model_manifests(self):
        logger.info('manifests')
        rowids = self._get_all_rowids()
        uuids = self.get_model_uuid(rowids)
        for rowid, uuid in zip(rowids, uuids):
            logger.info('rowid = %r' % (rowid,))
            logger.info(ut.repr3(self.get_model_inputs(uuid), nl=1))

    def _assert_self(self):
        assert len(self.data_colnames) == len(
            self.data_coltypes
        ), 'specify same number of colnames and coltypes'
        if self.preproc_func is not None:
            # Check that preproc_func has a valid signature
            # ie (depc, parent_ids, config)
            argspec = ut.get_func_argspec(self.preproc_func)
            args = argspec.args
            if argspec.varargs and (hasattr(argspec, 'keywords') and argspec.keywords):
                assert len(args) == 1, 'varargs and kwargs must have one arg for depcache'
            else:
                if len(args) < 3:
                    logger.info('args = %r' % (args,))
                    msg = (
                        'preproc_func=%r for table=%s must have a '
                        'depcache arg, at least one parent rowid arg, '
                        'and a config arg'
                    ) % (self.preproc_func, self.tablename)
                    raise AssertionError(msg)
                rowid_args = args[1:-1]
                if len(rowid_args) != len(self.parents()):
                    logger.info('self.preproc_func = %r' % (self.preproc_func,))
                    logger.info('args = %r' % (args,))
                    logger.info('rowid_args = %r' % (rowid_args,))
                    msg = (
                        'preproc function for table=%s must have as many '
                        'rowids %d args as parent %d'
                    ) % (self.tablename, len(rowid_args), len(self.parents()))
                    raise AssertionError(msg)
        extern_class_colattrs = [
            colattr for colattr in self.data_col_attrs if colattr.get('is_external_class')
        ]
        for colattr in extern_class_colattrs:
            cls = colattr['coltype']
            # Check external column class funcs
            argspec = ut.get_func_argspec(cls.__init__)
            if argspec.defaults is not None:
                num_nondefault = len(argspec.args) - len(argspec.defaults)
            else:
                num_nondefault = len(argspec.args)
            if num_nondefault > 1:
                msg = ut.codeblock(
                    """
                    External args must be able to be constructed without any
                    args. IE: You need a default __init__(self) method
                    """
                )
                raise AssertionError(msg)


@ut.reloadable_class
class _TableInternalSetup(ub.NiceRepr):
    """helper that sets up column information"""

    @profile
    def _infer_datacol(self):
        """
        Constructs the columns needed to represent relationship to data

        Infers interal properties about this table given the colnames and
        datatypes

        CommandLine:
            python -m dtool.depcache_table --exec-_infer_datacol --show

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache import testdata_depc
            >>> depc = testdata_depc()
            >>> for table in depc.tables:
            >>>     print('----')
            >>>     table._infer_datacol()
            >>>     print(table)
            >>>     print('table.data_col_attrs = %s' %
            >>>           ut.repr3(table.data_col_attrs, nl=8))
            >>> table = depc['probchip']
            >>> table = depc['spam']
            >>> table = depc['vsone']
        """
        data_col_attrs = []

        # Parse column datatypes
        _iter = enumerate(zip(self.data_colnames, self.data_coltypes))
        for data_colx, (colname, coltype) in _iter:
            colattr = ut.odict()
            # Check column input subtypes
            is_tuple = isinstance(coltype, tuple)
            is_func = ut.is_func_or_method(coltype)
            is_externtup = is_tuple and coltype[0] == 'extern'
            is_functup = is_tuple and ut.is_func_or_method(coltype[0])
            is_exttype = isinstance(coltype, ExternType)
            # Check column input main types
            is_normal = coltype in TYPE_TO_SQLTYPE
            # is_normal   = not (is_tuple or is_func)
            isnested = is_tuple and not (is_func or is_externtup)
            is_external = is_func or is_functup or is_externtup or is_exttype
            # Switch on input types
            colattr['colname'] = colname
            colattr['coltype'] = coltype
            colattr['data_colx'] = data_colx
            if is_normal:
                # Normal non-nested column
                sqltype = TYPE_TO_SQLTYPE[coltype]
                colattr['intern_colname'] = colname
                colattr['sqltype'] = sqltype
                colattr['is_normal'] = is_normal
            elif isnested:
                # Nested non-function normal columns
                colattr['isnested'] = isnested
                nestattrs = colattr['nestattrs'] = []
                for count, subtype in enumerate(coltype):
                    nestattr = ut.odict()
                    nestattrs.append(nestattr)
                    flat_colname = '%s_%d' % (colname, count)
                    sqltype = TYPE_TO_SQLTYPE[subtype]
                    nestattr['flat_colname'] = flat_colname
                    nestattr['sqltype'] = sqltype
            elif is_external:
                # Nested external funcs
                write_func = None
                if is_exttype:
                    read_func = coltype.read_func
                    write_func = coltype.write_func
                    if coltype.extern_ext is not None:
                        colattr['extern_ext'] = coltype.extern_ext
                    if coltype.extkey is not None:
                        colattr['extkey'] = coltype.extkey
                elif is_externtup:
                    read_func = coltype[1]
                    if len(coltype) > 2:
                        write_func = coltype[2]
                    if len(coltype) > 3:
                        colattr['extern_ext'] = coltype[3]
                elif is_functup:
                    read_func = coltype[0]
                else:
                    read_func = coltype
                intern_colname = colname + EXTERN_SUFFIX
                sqltype = TYPE_TO_SQLTYPE[str]
                colattr['is_external'] = True
                colattr['intern_colname'] = intern_colname
                colattr['write_func'] = write_func
                colattr['read_func'] = read_func
                colattr['sqltype'] = sqltype
            else:
                # External class column
                assert hasattr(coltype, '__getstate__') and hasattr(
                    coltype, '__setstate__'
                ), ('External classes must have __getstate__ and ' '__setstate__ methods')
                read_func, write_func = make_extern_io_funcs(self, coltype)
                sqltype = TYPE_TO_SQLTYPE[str]
                intern_colname = colname + EXTERN_SUFFIX
                # raise AssertionError('external class columns')
                colattr['is_external'] = True
                colattr['is_external_class'] = True
                colattr['coltype'] = coltype
                colattr['intern_colname'] = intern_colname
                colattr['write_func'] = write_func
                colattr['read_func'] = read_func
                colattr['sqltype'] = sqltype
            data_col_attrs.append(colattr)
        return data_col_attrs

    @profile
    def _infer_parentcol(self):
        """
        construct columns to represent relationship to parent

        CommandLine:
            python -m dtool.depcache_table _infer_parentcol --show

        Returns:
            list: list of dictionaries for each parent

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> table = depc['vsone']
            >>> table = depc['smk_match']
            >>> table = depc['neighbs']
            >>> table = depc['indexer']
            >>> parent_col_attrs = table._infer_parentcol()
            >>> result = ('parent_col_attrs = %s' % (ut.repr2(parent_col_attrs, nl=2),))
            >>> print(result)

        Ignore:
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> depc.d.get_indexer_data([1, 2, 3])
            >>> import uuid
            >>> depc.d.get_indexer_data([
            >>>     uuid.UUID('a01eda32-e4e0-b139-3274-e91d1b3e9ecf')])
        """
        parent_tablenames = self.parent_tablenames
        parent_col_attrs = []

        # Handle dependencies when a parent are pairwise between tables
        parent_id_prefixs1 = []
        parent_id_prefixs2 = []
        seen_ = ut.ddict(lambda: 1)

        for parent_colx, col in enumerate(parent_tablenames):
            colattr = ut.odict()
            # Detect multicolumns
            if col.endswith('*'):
                ismulti = True
                parent_table = col[:-1]
            else:
                ismulti = False
                parent_table = col
            colattr['col'] = col
            colattr['ismulti'] = ismulti
            # Local input-id helps specify branch ordering
            colattr['local_input_id'] = ''
            if ismulti:
                colattr['local_input_id'] += '*'
            colattr['parent_table'] = parent_table
            colattr['parent_colx'] = parent_colx
            parent_id_prefixs1.append(parent_table)
            parent_col_attrs.append(colattr)

        colhist = ut.dict_hist(parent_id_prefixs1)
        for parent_colx, col in enumerate(parent_id_prefixs1):
            colattr = parent_col_attrs[parent_colx]
            if colhist[col] > 1:
                # Duplicate column names recieve indicies
                nwise_idx = seen_[col]
                nwise_total = colhist[col]
                prefix = col + str(nwise_idx)
                seen_[col] += 1
                colattr['isnwise'] = True
                colattr['nwise_total'] = nwise_total
                colattr['nwise_idx'] = nwise_idx
                colattr['local_input_id'] += str(nwise_idx)
            else:
                if not colattr['local_input_id']:
                    colattr['local_input_id'] = '1'
                prefix = col
                colattr['isnwise'] = False
            colattr['prefix'] = prefix
            parent_id_prefixs2.append(prefix)

        # Handle case when parent are a set of ids
        for colattr, prefix in zip(parent_col_attrs, parent_id_prefixs2):
            column_ismulti = colattr['ismulti']
            if column_ismulti:
                # Case when dependencies are many to one hash of set items
                colname = prefix + '_setuuid'
                sqltype = 'UUID NOT NULL'
                INPUT_SIZE_SUFFIX = 'setsize'
                extra_cols = []
                extra_cols += [
                    {
                        'intern_colname': prefix + '_' + INPUT_SIZE_SUFFIX,
                        'sqltype': 'INTEGER NOT NULL',
                        # 'doc': 'size of an input set for this model',
                    }
                ]
                # File that maintains manifest of model inputs
                # INPUT_FPATH_SUFFIX = 'setfpath'
                # extra_cols += [{
                # 'intern_colname': prefix + '_' + INPUT_FPATH_SUFFIX,
                # 'sqltype': 'TEXT'
                # }]
                colattr['extra_cols'] = extra_cols
                # colattr['issuper'] = True
            else:
                # Normal case when dependencies are one to one
                colname = prefix + '_rowid'
                sqltype = 'INTEGER NOT NULL'
            colattr['intern_colname'] = colname
            colattr['sqltype'] = sqltype

        parent_col_attrs = [
            ut.order_dict_by(colattr, ['intern_colname', 'sqltype'])
            for colattr in parent_col_attrs
        ]
        return parent_col_attrs

    @profile
    def _infer_allcol(self):
        r"""
        Combine information from parentcol and datacol
        Build column definitions that will directly define SQL columns
        """
        internal_col_attrs = []

        # Append primary column
        colattr = ut.odict(
            [
                ('intern_colname', self.rowid_colname),
                ('sqltype', 'INTEGER PRIMARY KEY'),
                ('isprimary', True),
            ]
        )
        colattr['intern_colx'] = len(internal_col_attrs)
        internal_col_attrs.append(colattr)

        # Append parent columns
        ismulti = False
        for parent_colattr in self.parent_col_attrs:
            colattr = ut.odict()
            colattr['intern_colname'] = parent_colattr['intern_colname']
            colattr['parent_table'] = parent_colattr['parent_table']
            if parent_colattr['ismulti']:
                ismulti = True
            colattr['ismulti'] = parent_colattr['ismulti']
            colattr['isnwise'] = parent_colattr['isnwise']
            if colattr['isnwise']:
                colattr['nwise_total'] = parent_colattr['nwise_total']
                colattr['nwise_idx'] = parent_colattr['nwise_idx']
            colattr['sqltype'] = parent_colattr['sqltype']
            colattr['parent_colx'] = parent_colattr['parent_colx']
            colattr['intern_colx'] = len(internal_col_attrs)
            colattr['isparent'] = True
            colattr['issuper'] = True
            internal_col_attrs.append(colattr)

        # Append config columns
        colattr = ut.odict(
            [
                ('intern_colname', CONFIG_ROWID),
                ('sqltype', 'INTEGER DEFAULT 0'),
                ('issuper', True),
            ]
        )
        colattr['intern_colx'] = len(internal_col_attrs)
        internal_col_attrs.append(colattr)

        # Append quick access column
        # return any(self.get_parent_col_attr('ismulti'))
        # if self.ismulti:
        if ismulti:
            # Append model uuid column
            colattr = ut.odict()
            colattr['intern_colname'] = self.model_uuid_colname
            colattr['sqltype'] = 'UUID NOT NULL'
            colattr['intern_colx'] = len(internal_col_attrs)
            internal_col_attrs.append(colattr)

            # Append model uuid column
            colattr = ut.odict()
            colattr['intern_colname'] = self.is_augmented_colname
            colattr['sqltype'] = 'INTEGER DEFAULT 0'
            colattr['intern_colx'] = len(internal_col_attrs)
            internal_col_attrs.append(colattr)

            if False:
                # TODO: eventually enable
                if self.taggable:
                    colattr = ut.odict()
                    colattr['intern_colname'] = 'model_tag'
                    colattr['sqltype'] = 'TEXT'
                    colattr['intern_colx'] = len(internal_col_attrs)
                    internal_col_attrs.append(colattr)
                    pass
        else:
            # Append primary rowid column
            pass

        # Append data columns
        for data_colattr in self.data_col_attrs:
            colname = data_colattr['colname']
            if data_colattr.get('isnested', False):
                for nestcol in data_colattr['nestattrs']:
                    colattr = ut.odict()
                    colattr['intern_colname'] = nestcol['flat_colname']
                    colattr['sqltype'] = nestcol['sqltype']
                    colattr['intern_colx'] = len(internal_col_attrs)
                    colattr['data_colx'] = data_colattr['data_colx']
                    colattr['colname'] = colname
                    colattr['isdata'] = True
                    internal_col_attrs.append(colattr)
            else:
                colattr = ut.odict()
                colattr['intern_colname'] = data_colattr['intern_colname']
                colattr['sqltype'] = data_colattr['sqltype']
                colattr['intern_colx'] = len(internal_col_attrs)
                colattr['data_colx'] = data_colattr['data_colx']
                colattr['isdata'] = True
                colattr['colname'] = colname
                if data_colattr.get('is_external', False):
                    colattr['is_external_pointer'] = True
                    colattr['write_func'] = data_colattr['write_func']
                    colattr['read_func'] = data_colattr['read_func']
                internal_col_attrs.append(colattr)

        # Append extra columns
        for parent_colattr in self.parent_col_attrs:
            for extra_colattr in parent_colattr.get('extra_cols', []):
                colattr = ut.odict()
                colattr['intern_colname'] = extra_colattr['intern_colname']
                colattr['sqltype'] = extra_colattr['sqltype']
                colattr['intern_colx'] = len(internal_col_attrs)
                colattr['isextra'] = True
                internal_col_attrs.append(colattr)
        return internal_col_attrs


@ut.reloadable_class
class _TableGeneralHelper(ub.NiceRepr):
    """helper"""

    def __nice__(self):
        num_parents = len(self.parent_tablenames)
        num_cols = len(self.data_colnames)
        return '(%s) nP=%d%s nC=%d' % (
            self.tablename,
            num_parents,
            '*' if False and self.ismulti else '',
            num_cols,
        )

    # @property
    # def _table_colnames(self):
    #    return

    @property
    def extern_dpath(self):
        cache_dpath = self.depc.cache_dpath
        extern_dname = 'extern_' + self.tablename
        extern_dpath = join(cache_dpath, extern_dname)
        return extern_dpath

    @property
    def dpath(self):
        # assert table.ismulti, 'only valid for models'
        dname = self.tablename + '_storage'
        dpath = join(self.depc.cache_dpath, dname)
        # ut.ensuredir(dpath)
        return dpath

    # def dpath(self):
    #    from os.path import dirname
    #    dpath = dirname(self.db.fpath)
    #    return dpath

    @property
    @ut.memoize
    def ismulti(self):
        # TODO: or has multi parent
        return any(self.get_parent_col_attr('ismulti'))

    @property
    def configclass(self):
        return self.depc.configclass_dict[self.tablename]

    @property
    def requestclass(self):
        return self.depc.requestclass_dict.get(self.tablename, None)

    def new_request(self, qaids, daids, cfgdict=None):
        request = self.depc.new_request(self.tablename, qaids, daids, cfgdict=cfgdict)
        return request

    # --- Standard Properties

    @property
    def internal_data_col_attrs(self):
        flags = self.get_intern_col_attr('isdata')
        return ut.compress(self.internal_col_attrs, flags)

    @property
    def internal_parent_col_attrs(self):
        flags = self.get_intern_col_attr('isparent')
        return ut.compress(self.internal_col_attrs, flags)

    # --- / Standard Properties

    @ut.memoize
    def get_parent_col_attr(self, key):
        return ut.dict_take_column(self.parent_col_attrs, key)

    @ut.memoize
    def get_intern_data_col_attr(self, key):
        return ut.dict_take_column(self.internal_data_col_attrs, key)

    @ut.memoize
    def get_intern_parent_col_attr(self, key):
        return ut.dict_take_column(self.internal_parent_col_attrs, key)

    @ut.memoize
    def get_intern_col_attr(self, key):
        return ut.dict_take_column(self.internal_col_attrs, key)

    @ut.memoize
    def get_data_col_attr(self, key):
        return ut.dict_take_column(self.data_col_attrs, key)

    @property
    @ut.memoize
    def parent_id_tablenames(self):
        tablenames = tuple(
            [parent_colattr['parent_table'] for parent_colattr in self.parent_col_attrs]
        )
        return tablenames

    @property
    @ut.memoize
    def parent_id_prefix(self):
        prefixes = tuple(
            [parent_colattr['prefix'] for parent_colattr in self.parent_col_attrs]
        )
        return prefixes

    @property
    def extern_columns(self):
        colnames = self.get_data_col_attr('colname')
        flags = self.get_data_col_attr('is_extern')
        return ut.compress(colnames, flags)

    @property
    def rowid_colname(self):
        """rowid of this table used by other dependant tables"""
        return self.tablename + '_rowid'

    @property
    def superkey_colnames(self):
        return self.parent_id_colnames + (CONFIG_ROWID,)

    @property
    def model_uuid_colname(self):
        return 'model_uuid'

    @property
    def is_augmented_colname(self):
        return 'augment_bit'

    @property
    def parent_id_colnames(self):
        return tuple([colattr['intern_colname'] for colattr in self.parent_col_attrs])

    def get_rowids_from_root(self, root_rowids, config=None):
        return self.depc.get_rowids(self.tablename, root_rowids, config=config)

    @property
    @ut.memoize
    def parent(self):
        return ut.odict(
            [
                (parent_colattr['parent_table'], parent_colattr)
                for parent_colattr in self.parent_col_attrs
            ]
        )
        # return tuple([parent_colattr['parent_table']
        #              for parent_colattr in self.parent_col_attrs])

    @ut.memoize
    def parents(self, data=None):
        if data:
            return [
                (parent_colattr['parent_table'], parent_colattr)
                for parent_colattr in self.parent_col_attrs
            ]
        else:
            return [
                parent_colattr['parent_table'] for parent_colattr in self.parent_col_attrs
            ]

    @property
    def children(self):
        graph = self.depc.explicit_graph
        children_tablenames = list(nx.neighbors(graph, self.tablename))
        return children_tablenames

    @property
    def ancestors(self):
        graph = self.depc.explicit_graph
        children_tablenames = list(nx.ancestors(graph, self.tablename))
        return children_tablenames

    def show_dep_subgraph(self, inter=None):
        from wbia.plottool.interactions import ExpandableInteraction

        autostart = inter is None
        if inter is None:
            inter = ExpandableInteraction(nCols=2)
        import wbia.plottool as pt

        graph = self.depc.explicit_graph
        nodes = ut.nx_all_nodes_between(graph, None, self.tablename)
        G = graph.subgraph(nodes)

        plot_kw = {'fontname': 'Ubuntu'}
        inter.append_plot(
            ut.partial(
                pt.show_nx,
                G,
                title='Dependency Subgraph (%s)' % (self.tablename),
                **plot_kw,
            )
        )
        if autostart:
            inter.start()

    def show_input_graph(self, inter=None):
        """
        CommandLine:
            python -m dtool.depcache_table show_input_graph --show

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3()
            >>> # xdoctest: +REQUIRES(--show)
            >>> ut.quit_if_noshow()
            >>> import wbia.plottool as pt
            >>> table = depc['smk_match']
            >>> table.show_input_graph()
            >>> #print(depc['smk_match'].flat_compute_rmi_edges)
            >>> ut.show_if_requested()
        """
        from wbia.plottool.interactions import ExpandableInteraction

        autostart = inter is None
        if inter is None:
            inter = ExpandableInteraction(nCols=2)
        self.show_dep_subgraph(inter)
        inputs = self.rootmost_inputs
        inter = inputs.show_exi_graph(inter)
        if autostart:
            inter.start()
        return inter

    @property
    @ut.memoize
    def expanded_input_graph(self):
        """
        CommandLine:
            python -m dtool.depcache_table --exec-expanded_input_graph --show --table=neighbs
            python -m dtool.depcache_table --exec-expanded_input_graph --show --table=vsone
            python -m dtool.depcache_table --exec-expanded_input_graph --show --table=smk_match

        TODO:
            * determine root argument structure
            * ???
            * compute dependencies in order

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_control import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import * # NOQA
            >>> depc = testdata_depc3()
            >>> tablename = ut.get_argval('--table', default='vsone')
            >>> table = depc[tablename]
            >>> # xdoctest: +REQUIRES(--show)
            >>> import wbia.plottool as pt
            >>> pt.ensureqt()
            >>> table.show_input_graph()
            >>> pt.interactions.zoom_factory()
            >>> ut.show_if_requested()
        """
        from wbia.dtool import input_helpers

        graph = self.depc.explicit_graph.copy()
        target = self.tablename
        exi_graph = input_helpers.make_expanded_input_graph(graph, target)
        return exi_graph

    @property
    def rootmost_inputs(self):
        """
        CommandLine:
            python -m dtool.depcache_table rootmost_inputs --show

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.depcache_control import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3()
            >>> #tablename = 'multitest_score'
            >>> tablename = 'smk_match'
            >>> table = depc[tablename]
            >>> inputs = table.rootmost_inputs
            >>> result = ('inputs = %s' % (inputs,))
            >>> print('compute_order = %s' % (ut.repr2(inputs.flat_compute_rmi_edges(), nl=1)))
            ......
            >>> print(result)
            inputs = <TableInput [annot[t], vocab[t], inv_index[t]]>
        """
        from wbia.dtool import input_helpers

        exi_graph = self.expanded_input_graph
        rootmost_inputs = input_helpers.get_rootmost_inputs(exi_graph, self)
        return rootmost_inputs

    @ut.memoize
    def requestable_col_attrs(self):
        """
        Maps names of requestable columns to indicies of internal columns
        """
        requestable_col_attrs = {}
        for colattr in self.internal_data_col_attrs:
            rattr = {}
            colname = colattr['intern_colname']
            rattr['intern_colx'] = colattr['intern_colx']
            rattr['intern_colname'] = colattr['intern_colname']
            requestable_col_attrs[colname] = rattr

        for colattr in self.data_col_attrs:
            rattr = {}
            if colattr.get('isnested'):
                nest_internal_names = ut.take_column(colattr['nestattrs'], 'flat_colname')
                nest_attrs = ut.dict_take(requestable_col_attrs, nest_internal_names)
                rattr['intern_colname'] = nest_internal_names
                rattr['intern_colx'] = ut.take_column(nest_attrs, 'intern_colx')
                rattr['isnested'] = True
            elif colattr.get('is_external'):
                intern_attr = requestable_col_attrs[colattr['intern_colname']]
                rattr['intern_colname'] = intern_attr['intern_colname']
                rattr['intern_colx'] = intern_attr['intern_colx']
                rattr['read_func'] = colattr['read_func']
                rattr['write_func'] = colattr['write_func']
                rattr['is_extern'] = True
            else:
                continue
            colname = colattr['colname']
            rattr['colname'] = colname
            requestable_col_attrs[colname] = rattr
        return requestable_col_attrs

    @ut.memoize
    def computable_colnames(self):
        # These are the colnames that we expect to be computed
        intern_colnames = ut.take_column(self.internal_col_attrs, 'intern_colname')
        insertable_flags = [
            not colattr.get('isprimary') for colattr in self.internal_col_attrs
        ]
        colnames = tuple(ut.compress(intern_colnames, insertable_flags))
        return colnames


@ut.reloadable_class
class _TableComputeHelper(object):
    """helper for computing functions"""

    # @profile
    def prepare_storage(
        self, dirty_parent_ids, proptup_gen, dirty_preproc_args, config_rowid, config
    ):
        """
        Converts output from ``preproc_func`` to data that can be stored in SQL

        CommandLine:
            python -m dtool.depcache_table prepare_storage

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3(in_memory=False)
            >>> depc.clear_all()
            >>> tablename = 'labeler'
            >>> tablename = 'indexer'
            >>> config = {tablename + '_param': None, 'foo': 'bar'}
            >>> data = depc.get('labeler', [1, 2, 3], 'data')
            >>> data = depc.get('labeler', [1, 2, 3], 'data', config=config)
            >>> data = depc.get('indexer', [[1, 2, 3]], 'data')
            >>> data = depc.get('indexer', [[1, 2, 3]], 'data', config=config)
            >>> rowids = depc.get_rowids('indexer', [[1, 2, 3]],  config=config)
            >>> table = depc[tablename]
            >>> model_uuid_list = table.get_internal_columns(rowids, ('model_uuid',))
            >>> model_uuid = model_uuid_list[0]
            >>> rowids2 = table.get_model_rowids(model_uuid_list)
            >>> assert rowids == rowids2, 'bad rowid computation'
            >>> table.print_table()
            >>> table.print_internal_info()
            >>> table.print_configs()
            >>> table.print_model_manifests()
            >>> #ut.vd(depc.cache_dpath)
        """
        if self.default_to_unpack:
            # Hack for tables explicilty specified with a single column
            proptup_gen = (None if data is None else (data,) for data in proptup_gen)
        # Flatten nested columns
        if any(self.get_data_col_attr('isnested')):
            proptup_gen = self._prepare_storage_nested(proptup_gen)
        # Write external columns
        if any(self.get_data_col_attr('write_func')):
            proptup_gen = self._prepare_storage_extern(
                dirty_parent_ids, config_rowid, config, proptup_gen
            )
        if self.ismulti:
            manifest_dpath = self.dpath
            ut.ensuredir(manifest_dpath)
        # Concatenate data with internal rowids / config-id
        for ids_, data_cols, args_ in zip(
            dirty_parent_ids, proptup_gen, dirty_preproc_args
        ):
            try:
                if data_cols is None:
                    yield None
                else:
                    multi_parent_flags = self.get_parent_col_attr('ismulti')
                    parent_colnames = self.get_parent_col_attr('intern_colname')
                    multi_id_names = ut.compress(parent_colnames, multi_parent_flags)
                    multi_ids = ut.compress(ids_, multi_parent_flags)
                    multi_args = ut.compress(args_, multi_parent_flags)

                    if self.ismulti:
                        multi_setsizes = []
                        manifest_data = {}
                        for multi_id, arg_, name in zip(
                            multi_ids, multi_args, multi_id_names
                        ):
                            assert self.ismulti, 'only valid for models'
                            # TODO: need to get back to root ids
                            manifest_data.update(
                                **{
                                    name + '_multi_id': multi_id,
                                    name + '_primary_ids': 'FIXME' + str(arg_),
                                    name + '_model_input': list(arg_),
                                }
                            )
                            multi_setsizes.append(len(arg_))

                        # Make a new model uuid
                        # TODO: maybe we should not do this here
                        model_uuid = ut.hashable_to_uuid((multi_ids, config.get_cfgstr()))
                        manifest_data['config'] = config
                        manifest_data['model_uuid'] = model_uuid
                        manifest_data['augmented'] = False

                        manifest_fpath = self.get_model_manifest_fpath(model_uuid)
                        ut.save_json(manifest_fpath, manifest_data, pretty=1)

                        # TODO: hash all input UUIDs and the full config together
                        quick_access_tup = (model_uuid, 0)
                        # Give the setsize and setfpath data if needed
                        parent_extra = tuple(ut.flatten(zip(multi_setsizes)))
                    else:
                        quick_access_tup = tuple()
                        parent_extra = tuple()
                    # parent_extra = tuple(ut.flatten(zip(multi_setsizes, multi_setfpaths)))
                    # parent_extra = tuple(ut.flatten([(len(arg), fname) for arg,
                    #                                 fname in zip(multi_args,
                    #                                              multi_fpaths)]))
                    row_tup = (
                        tuple(ids_)
                        + (config_rowid,)
                        + quick_access_tup
                        + data_cols
                        + parent_extra
                    )
                    # logger.info('row_tup = %r' % (row_tup,))
                    yield row_tup
            except Exception as ex:
                ut.printex(
                    ex, 'cat error', keys=['config_rowid', 'data_cols', 'parent_rowids']
                )
                raise

    def get_model_manifest_fname(self, model_uuid):
        manifest_fname = 'input_manifest_%s.json' % (model_uuid,)
        return manifest_fname

    def get_model_manifest_fpath(self, model_uuid):
        manifest_fname = self.get_model_manifest_fname(model_uuid)
        manifest_fpath = join(self.dpath, manifest_fname)
        return manifest_fpath

    def get_model_inputs(self, model_uuid):
        """
        Ignore:
            >>> table.get_model_uuid([2])
            [UUID('5b66772c-e654-dd9a-c9de-0ccc1bb6861c')]
        """
        assert self.ismulti, 'must be a model'
        manifest_fpath = self.get_model_manifest_fpath(model_uuid)
        manifest_data = ut.load_json(manifest_fpath)
        return manifest_data

    def get_model_uuid(self, rowids):
        """
        Ignore:
            >>> table.get_model_uuid([2])
            [UUID('5b66772c-e654-dd9a-c9de-0ccc1bb6861c')]
        """
        assert self.ismulti, 'must be a model'
        model_uuid_list = self.get_internal_columns(rowids, ('model_uuid',))
        return model_uuid_list

    def get_model_rowids(self, model_uuid_list):
        """
        Get the rowid of a model given its uuid

        Ignore:
            >>> import uuid
            >>> table.get_model_rowids([uuid.UUID('5b66772c-e654-dd9a-c9de-0ccc1bb6861c')])
            [2]
        """
        assert self.ismulti, 'must be a model'
        colnames = (self.rowid_colname,)
        andwhere_colnames = (self.model_uuid_colname,)
        params_iter = list(zip(model_uuid_list))
        rowid_list = self.db.get_where_eq(
            self.tablename,
            colnames,
            params_iter,
            andwhere_colnames,
            eager=True,
            nInput=len(model_uuid_list),
        )
        return rowid_list

    @profile
    def _prepare_storage_nested(self, proptup_gen):
        """
        Hack for when a sql schema has tuples defined in it.
        Accepts nested tuples and flattens them to fit into the sql tables
        """
        nCols = len(self.data_colnames)
        idxs1 = ut.where(self.get_data_col_attr('isnested'))
        idxs2 = ut.index_complement(idxs1, nCols)
        for data in proptup_gen:
            if data is None:
                yield None
                continue
            # Split data into nested and unnested columns
            unnested_cols = list(zip(ut.take(data, idxs2)))
            nested_cols = ut.take(data, idxs1)
            grouped_items = [nested_cols, unnested_cols]
            groupxs = [idxs1, idxs2]
            # Flatten nested columns
            unflat = ut.ungroup(grouped_items, groupxs, nCols - 1)
            # Recombine the data
            data_new = tuple(ut.flatten(unflat))
            yield data_new

    # @profile
    def _prepare_storage_extern(
        self, dirty_parent_ids, config_rowid, config, proptup_gen
    ):
        """
        Writes external data to disk if write function is specified.
        """
        internal_data_col_attrs = self.internal_data_col_attrs
        writable_flags = ut.dict_take_column(internal_data_col_attrs, 'write_func', False)
        extern_colattrs = ut.compress(internal_data_col_attrs, writable_flags)
        # extern_colnames = ut.dict_take_column(extern_colattrs, 'colname')
        extern_writers = ut.dict_take_column(extern_colattrs, 'write_func')

        nCols = len(internal_data_col_attrs)
        idxs1 = ut.where(writable_flags)
        idxs2 = ut.index_complement(idxs1, nCols)
        extern_fnames_list = list(
            zip(
                *[
                    self._get_extern_fnames(
                        dirty_parent_ids, config_rowid, config, extern_colattr
                    )
                    for extern_colattr in extern_colattrs
                ]
            )
        )
        # get extern cache directory and fpaths
        extern_dpath = self.extern_dpath
        ut.ensuredir(extern_dpath)
        # extern_fpaths_list = [
        #     [join(extern_dpath, fname) for fname in fnames]
        #     for fnames in extern_fnames_list
        # ]

        for data, extern_fpaths in zip(proptup_gen, extern_fnames_list):
            if data is None:
                yield None
                continue
            normal_data = ut.take(data, idxs2)
            try:
                extern_data = ut.take(data, idxs1)
            except Exception as ex:
                ut.printex(ex, 'Did you forget to return/yeild your data as a tuple?')
                raise
            # Write external data to disk
            try:
                _iter = zip(extern_data, extern_fpaths, extern_writers)
                for obj, fpath, write_func in _iter:
                    abs_fpath = join(extern_dpath, fpath)
                    # logger.info('WRITE fpath = %r, abs_fpath = %r' % (fpath, abs_fpath, ))
                    write_func(abs_fpath, obj)
                    ut.assert_exists(abs_fpath, verbose=False)
            except Exception as ex:
                ut.printex(ex, 'external write', keys=['config_rowid', 'data'])
                raise
            # Return path instead of data
            grouped_items = [extern_fpaths, normal_data]
            groupxs = [idxs1, idxs2]
            data_new = tuple(ut.ungroup(grouped_items, groupxs, nCols - 1))
            yield data_new

    def get_extern_fnames(self, parent_rowids, config, extern_col_index=0):
        """
        convinience function around get_extern_fnames

        Exmaple:
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> import wbia
            >>> ibs = wbia.opendb(defaultdb='testdb1')
            >>> depc = ibs.depc_annot
            >>> tablename = 'chips'
            >>> table = depc[tablename]
            >>> extern_col_index = 0
            >>> info_props = ['image_uuid', 'verts', 'theta']
            >>> config = depc.configclass_dict[tablename]()
            >>> root_rowids = [1, 2, 3]
            >>> rowid_list = depc.get_rowids(tablename, root_rowids)
            >>> parent_rowids = table.get_parent_rowids(rowid_list)
            >>> fname_list = table.get_extern_fnames(parent_rowids, config)
            >>> print('fname_list = %r' % (fname_list,))
        """
        config_rowid = self.get_config_rowid(config)
        # depc.get_rowids(tablename, root_rowids, config)
        internal_data_col_attrs = self.internal_data_col_attrs
        writable_flags = ut.dict_take_column(internal_data_col_attrs, 'write_func', False)
        extern_colattrs = ut.compress(internal_data_col_attrs, writable_flags)
        extern_colattr = extern_colattrs[extern_col_index]
        fname_list = self._get_extern_fnames(
            parent_rowids, config_rowid, config, extern_colattr
        )

        # if False:
        #    root_rowids = self.depc.get_root_rowids(self.tablename, rowid_list)
        #    info_props = ['image_uuid', 'verts', 'theta']
        #    self.depc.make_root_info_uuid(root_rowids, info_props)

        return fname_list

    def _get_extern_fnames(
        self, parent_rowids, config_rowid, config, extern_colattr=None
    ):
        """
        TODO:
            * Clean up signature
            * Make this function return the filenames used
              by a specific external column in this table.  The inputs are the
              parent_rowids, (and the root rowids?), and the config.

        Args:
            parent_rowids (list of tuples) - list of tuples of rowids
        """
        config_hashid = self.get_config_hashid([config_rowid])[0]
        prefix = self.tablename
        prefix += '_' + extern_colattr['colname']
        colattrs = self.data_col_attrs[extern_colattr['data_colx']]
        # if colname is not None:
        #    prefix += '_' + colname
        # TODO: Put relevant root properties into the hash of the filename
        # (like bbox, parent image. basically the general vuuid and suuid.
        fmtstr = '{prefix}_id={rowids}_{config_hashid}{ext}'
        # HACK: check if the config specifies the extension type
        # extkey = self.extern_ext_config_keys.get(colname, 'ext')
        if 'extern_ext' in colattrs:
            ext = colattrs['extern_ext']
        else:
            extkey = colattrs.get('extkey', 'ext')
            ext = config[extkey] if extkey in config else '.cPkl'
        fname_list = [
            fmtstr.format(
                prefix=prefix,
                rowids='_'.join(list(map(str, rowids))),
                config_hashid=config_hashid,
                ext=ext,
            )
            for rowids in parent_rowids
        ]
        return fname_list

    def _compute_dirty_rows(
        self, dirty_parent_ids, dirty_preproc_args, config_rowid, config, verbose=True
    ):
        """
        dirty_preproc_args = preproc_args
        dirty_parent_ids = parent_rowids
        config_ = config
        """
        nInput = len(dirty_parent_ids)
        # if verbose:
        #     logger.info('[deptbl.compute] nInput = %r' % (nInput,))

        # Pack arguments into column-wise order to send to the func
        argsT = zip(*dirty_preproc_args)
        argsT = list(argsT)  # TODO: remove

        # HACK extract config if given a request
        config_ = config.config if hasattr(config, 'config') else config

        # call registered worker function
        if self.vectorized:
            # Function is written in a way that only accepts multiple inputs at
            # once and generates output
            proptup_gen = self.preproc_func(self.depc, *argsT, config=config_)
        else:
            # Function is written in a way that only accepts a single row of
            # input at a time
            proptup_gen = (
                self.preproc_func(self.depc, *argrow, config=config_)
                for argrow in zip(*argsT)
            )

        DEBUG_LIST_MODE = True
        if DEBUG_LIST_MODE:
            proptup_gen = list(proptup_gen)
            num_output = len(proptup_gen)
            assert (
                num_output == nInput
            ), 'Input and output sizes do not agree. ' 'num_output=%r, num_input=%r' % (
                num_output,
                nInput,
            )
        # Append rowids and rectify nested and external columns
        dirty_params_iter = self.prepare_storage(
            dirty_parent_ids, proptup_gen, dirty_preproc_args, config_rowid, config_
        )
        if DEBUG_LIST_MODE:
            dirty_params_iter = list(dirty_params_iter)
            assert len(dirty_params_iter) == nInput
        # None data means that there was an error for a specific row
        return dirty_params_iter

    def _chunk_compute_dirty_rows(
        self, dirty_parent_ids, dirty_preproc_args, config_rowid, config, verbose=True
    ):
        """
        Executes registered functions, does external storage and yeilds results
        to be stored internally in SQL.

        CommandLine:
            python -m dtool.depcache_table _chunk_compute_dirty_rows

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3(in_memory=False)
            >>> depc.clear_all()
            >>> data = depc.get('labeler', [1, 2, 3], 'data')
            >>> data = depc.get('indexer', [[1, 2, 3]], 'data')
            >>> depc.print_all_tables()
        """
        nInput = len(dirty_parent_ids)
        chunksize = nInput if self.chunksize is None else self.chunksize

        logger.info(
            '[deptbl.compute] nInput={}, chunksize={}, tbl={}'.format(
                nInput, self.chunksize, self.tablename
            )
        )

        # Report computation progress
        dirty_iter = list(zip(dirty_parent_ids, dirty_preproc_args))
        prog_iter = ut.ProgChunks(
            dirty_iter,
            chunksize,
            nInput,
            lbl='[deptbl.compute] add %s chunk' % (self.tablename),
        )
        # These are the colnames that we expect to be computed
        colnames = self.computable_colnames()
        # CALL EXTERNAL PREPROCESSING / GENERATION FUNCTION
        try:
            # prog_iter = list(prog_iter)
            for dirty_chunk in tqdm.tqdm(prog_iter):
                nChunkInput = len(dirty_chunk)
                if nChunkInput == 0:
                    return
                dirty_parent_ids_chunk, dirty_preproc_args_chunk = zip(*dirty_chunk)

                dirty_params_iter = self._compute_dirty_rows(
                    dirty_parent_ids_chunk,
                    dirty_preproc_args_chunk,
                    config_rowid,
                    config,
                )

                DEBUG_LIST_MODE = True
                if DEBUG_LIST_MODE:
                    dirty_params_iter = list(dirty_params_iter)
                    assert len(dirty_params_iter) == nChunkInput
                # TODO: Separate into func which can be specified as a callback.
                # None data means that there was an error for a specific row
                dirty_params_iter = ut.filter_Nones(dirty_params_iter)
                nChunkInput = len(dirty_params_iter)
                yield colnames, dirty_params_iter, nChunkInput
        except Exception as ex:
            ut.printex(
                ex,
                'error in add_rowids',
                keys=[
                    'table',
                    'self.parents()',
                    'config',
                    'argsT',
                    'config_rowid',
                    'dirty_parent_ids',
                    'self.preproc_func',
                ],
                tb=True,
            )
            raise


@ut.reloadable_class
class DependencyCacheTable(
    _TableGeneralHelper,
    _TableInternalSetup,
    _TableDebugHelper,
    _TableComputeHelper,
    _TableConfigHelper,
):
    r"""
    An individual node in the dependency graph.

    All SQL column information is stored in:
        internal_col_attrs - keeps track of internal info

    Additional metadata about specific columns is stored in
        parent_col_attrs - keeps track of parent info
        data_col_attrs - keeps track of computed data

    Attributes:
        db (dtool.SQLDatabaseController): pointer to underlying database
        depc (dtool.DependencyCache): pointer to parent cache
        tablename (str): name of the table
        docstr (str): documentation for table
        parent_tablenames (str): parent tables in depcache
        data_colnames (List[str]): columns produced by preproc_func
        data_coltypes (List[str]): column SQL types produced by preproc_func
        preproc_func (func): worker function
        vectorized (bool): by defaults it is assumed registered functions can
            process multiple inputs at once.
        taggable (bool): specifies if a computed object can be disconected from
            its ancestors and accessed via a tag.

    CommandLine:
        python -m dtool.depcache_table --exec-DependencyCacheTable

    Example:
        >>> # ENABLE_DOCTEST
        >>> from wbia.dtool.depcache_table import *  # NOQA
        >>> from wbia.dtool.example_depcache import testdata_depc
        >>> depc = testdata_depc()
        >>> print(depc['vsmany'])
        >>> print(depc['spam'])
        >>> print(depc['vsone'])
        >>> print(depc['nnindexer'])
    """

    @profile
    def __init__(
        self,
        depc=None,
        parent_tablenames=None,
        tablename=None,
        data_colnames=None,
        data_coltypes=None,
        preproc_func=None,
        docstr='no docstr',
        fname=None,
        asobject=False,
        chunksize=None,
        isinteractive=False,  # no-op
        default_to_unpack=False,
        default_onthefly=False,  # no-op
        rm_extern_on_delete=False,
        vectorized=True,
        taggable=False,
    ):
        """
        recieves kwargs from depc._register_prop
        """
        try:
            self.db = None
        except Exception:
            # HACK: jedi type hinting. Need to have non-obvious condition
            self.db = SQLDatabaseController()
        assert (
            re.search('[0-9]', tablename) is None
        ), 'tablename=%r cannot contain numbers' % (tablename,)
        # parent depcache
        self.depc = depc
        # Definitions
        self.tablename = tablename
        self.docstr = docstr
        self.parent_tablenames = parent_tablenames
        self.data_colnames = tuple(data_colnames)
        self.data_coltypes = data_coltypes
        self.preproc_func = preproc_func
        self._db_name = fname
        # Behavior
        self.on_delete = None
        self.default_to_unpack = default_to_unpack
        self.vectorized = vectorized
        self.taggable = taggable

        # self.store_modification_time = True
        # Use the filesystem to accomplish this
        # self.store_access_time = True
        # self.store_create_time = True
        # self.store_delete_time = True

        self.chunksize = chunksize
        # SQL Internals
        self.sqldb_fpath = None
        self.rm_extern_on_delete = rm_extern_on_delete
        # Update internals
        self.parent_col_attrs = self._infer_parentcol()
        self.data_col_attrs = self._infer_datacol()
        self.internal_col_attrs = self._infer_allcol()
        # Check for errors

        if ut.SUPER_STRICT:
            self._assert_self()

        # ??? Clearly a hack, but to what end?
        self._hack_chunk_cache = None

    @classmethod
    def from_name(
        cls,
        db_name,
        table_name,
        depcache_controller,
        parent_tablenames=None,
        data_colnames=None,
        data_coltypes=None,
        preproc_func=None,
        docstr='no docstr',
        asobject=False,
        chunksize=None,
        default_to_unpack=False,
        rm_extern_on_delete=False,
        vectorized=True,
        taggable=False,
    ):
        """Build the instance based on a database and table name."""
        self = cls.__new__(cls)

        self._db_name = db_name

        # Set the table name
        if re.search('[0-9]', table_name):
            raise ValueError(f"tablename = '{table_name}' cannot contain numbers")
        self.tablename = table_name

        # Set the parent depcache controller
        self.depc = depcache_controller

        # Definitions
        self.docstr = docstr
        self.parent_tablenames = parent_tablenames
        self.data_colnames = tuple(data_colnames)
        self.data_coltypes = data_coltypes
        self.preproc_func = preproc_func
        #: Optional specification of the amount of blobs to modify in one SQL operation
        self.chunksize = chunksize

        # FIXME (20-Oct-12020) This definition of behavior by external means is a scope issue
        #       Another object should not be directly manipulating this object.
        #: functions defined and populated through DependencyCache._register_subprop
        self.subproperties = {}

        # Behavior
        self.on_delete = None
        self.default_to_unpack = default_to_unpack
        self.vectorized = vectorized
        self.taggable = taggable
        #: Flag to enable the deletion of external files on associated SQL row deletion.
        self.rm_extern_on_delete = rm_extern_on_delete

        # XXX (20-Oct-12020) It's not clear if these attributes are absolutely necessary.
        # Update internals
        self.parent_col_attrs = self._infer_parentcol()
        self.data_col_attrs = self._infer_datacol()
        self.internal_col_attrs = self._infer_allcol()
        # /XXX

        # Check for errors
        # FIXME (20-Oct-12020) This seems like a bad idea...
        #       Why would you sometimes want to check and not at other times?
        if ut.SUPER_STRICT:
            self._assert_self()

        # ??? Clearly a hack, but to what end?
        self._hack_chunk_cache = None

        return self

    @property
    def fname(self):
        """Backwards compatible name of the database this Table belongs to"""
        # BBB (20-Oct-12020) 'fname' is the legacy name for the database name
        return self._db_name

    def initialize(self, _debug=None):
        """
        Ensures the SQL schema for this cache table
        """
        self.db = self.depc.get_db_by_name(self._db_name)
        # logger.info('Checking sql for table=%r' % (self.tablename,))
        if not self.db.has_table(self.tablename):
            logger.debug('Initializing table=%r' % (self.tablename,))
            new_state = self._get_addtable_kw()
            self.db.add_table(**new_state)
        else:
            # TODO: Check for table modifications
            new_state = self._get_addtable_kw()
            try:
                current_state = self.db.get_table_autogen_dict(self.tablename)
            except Exception as ex:
                strict = True
                ut.printex(
                    ex,
                    'TABLE %s IS CORRUPTED' % (self.tablename,),
                    iswarning=not strict,
                )
                if strict:
                    raise
                self.clear_table()
                current_state = self.db.get_table_autogen_dict(self.tablename)

            results = compare_coldef_lists(
                current_state['coldef_list'], new_state['coldef_list']
            )
            if results:
                current_coldef, new_coldef = results
                raise TableOutOfSyncError(
                    self.db,
                    self.tablename,
                    f'Current schema: {current_coldef} Expected schema: {new_coldef}',
                )

    def _get_addtable_kw(self):
        """
        Information that defines the SQL table

        CommandLine:
            python -m dtool.depcache_table _get_addtable_kw

        Example:
            >>> # DISABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> table1 = depc['indexer']
            >>> table2 = depc['neighbs']
            >>> add_table_kw1 = table1._get_addtable_kw()
            >>> add_table_kw2 = table2._get_addtable_kw()
            >>> result1 = ('%s.add_table_kw = %s' % (table1.tablename, ut.repr2(add_table_kw1, nl=2),))
            >>> result2 = ('%s.add_table_kw = %s' % (table2.tablename, ut.repr2(add_table_kw2, nl=2),))
            >>> print(result1)
            >>> print(result2)
        """
        coldef_list = [
            (colattr['intern_colname'], colattr['sqltype'])
            for colattr in self.internal_col_attrs
        ]
        superkeys = [self.superkey_colnames]
        add_table_kw = ut.odict(
            [
                ('tablename', self.tablename),
                ('coldef_list', coldef_list),
                ('docstr', self.docstr),
                ('superkeys', superkeys),
                ('dependson', self.parents()),
            ]
        )
        return add_table_kw

    # ----------------------
    # --- GETTERS NATIVE ---
    # ----------------------

    def _get_all_rowids(self):
        return self.db.get_all_rowids(self.tablename)

    @property
    def number_of_rows(self):
        return self.db.get_row_count(self.tablename)

    # @profile
    def ensure_rows(
        self,
        parent_ids_,
        preproc_args,
        config=None,
        verbose=True,
        _debug=None,
        retry=3,
        retry_delay_min=1,
        retry_delay_max=10,
    ):
        """
        Lazy addition

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> table = depc['vsone']
            >>> exec(ut.execstr_funckw(table.get_rowid), globals())
            >>> config = table.configclass()
            >>> verbose = True
            >>> # test duplicate inputs are detected and accounted for
            >>> parent_rowids = [(i, i) for i in list(range(100))] * 100
            >>> rectify_tup = table._rectify_ids(parent_rowids)
            >>> (parent_ids_, preproc_args, idxs1, idxs2) = rectify_tup
            >>> rowids = table.ensure_rows(parent_ids_, preproc_args, config=config)
            >>> result = ('rowids = %r' % (rowids,))
            >>> print(result)
        """
        try:
            # Get requested configuration id
            config_rowid = self.get_config_rowid(config)

            # Check which rows are already computed
            initial_rowid_list = self._get_rowid(parent_ids_, config=config)
            initial_rowid_list = list(initial_rowid_list)

            logger.debug(
                '[deptbl.ensure] initial_rowid_list = %s'
                % (ut.trunc_repr(initial_rowid_list),)
            )
            logger.debug('[deptbl.ensure] config_rowid = %r' % (config_rowid,))

            # Get corresponding "dirty" parent rowids
            isdirty_list = ut.flag_None_items(initial_rowid_list)
            num_dirty = sum(isdirty_list)
            num_total = len(parent_ids_)

            if num_dirty > 0:
                logger.debug(
                    'Add %d / %d new rows to %r' % (num_dirty, num_total, self.tablename)
                )
                logger.debug(
                    '[deptbl.add]  * config_rowid = {}, config={}'.format(
                        config_rowid, str(config)
                    )
                )

                dirty_parent_ids_ = ut.compress(parent_ids_, isdirty_list)
                dirty_preproc_args_ = ut.compress(preproc_args, isdirty_list)

                # Process only unique items
                unique_flags = ut.flag_unique_items(dirty_parent_ids_)
                dirty_parent_ids = ut.compress(dirty_parent_ids_, unique_flags)
                dirty_preproc_args = ut.compress(dirty_preproc_args_, unique_flags)

                # Break iterator into chunks
                if False and verbose:
                    # check parent configs we are working with
                    for x, parname in enumerate(self.parents()):
                        if parname == self.depc.root:
                            continue
                        parent_table = self.depc[parname]
                        ut.take_column(parent_ids_, x)
                        rowid_list = ut.take_column(parent_ids_, x)
                        try:
                            parent_history = parent_table.get_config_history(rowid_list)
                            logger.info('parent_history = %r' % (parent_history,))
                        except KeyError:
                            logger.info(
                                '[depcache_table] WARNING: config history is having troubles... says Jon'
                            )

                # Gives the function a hacky cache to use between chunks
                self._hack_chunk_cache = {}
                gen = self._chunk_compute_dirty_rows(
                    dirty_parent_ids, dirty_preproc_args, config_rowid, config
                )
                for colnames, dirty_params_iter, nChunkInput in gen:
                    self.db._add(
                        self.tablename,
                        colnames,
                        dirty_params_iter,
                        nInput=nChunkInput,
                    )

                # Remove cache when main add is done
                self._hack_chunk_cache = None
                logger.debug('[deptbl.add] finished add')
                #
                # The requested data is clean and must now exist in the parent
                # database, do a lookup to ensure the correct order.
                rowid_list = self._get_rowid(parent_ids_, config=config)
            else:
                rowid_list = initial_rowid_list
            logger.debug('[deptbl.add] rowid_list = %s' % ut.trunc_repr(rowid_list))
        except lite.IntegrityError:
            if retry <= 0:
                raise

            logger.error(
                'DEPC ENSURE_ROWS FOR TABLE %r FAILED DUE TO INTEGRITY ERROR (RETRY %d)!'
                % (self, retry)
            )
            retry_delay = random.uniform(retry_delay_min, retry_delay_max)
            logger.error('\t WAITING %0.02f SECONDS THEN RETRYING' % (retry_delay,))
            time.sleep(retry_delay)

            retry_ = retry - 1
            rowid_list = self.ensure_rows(
                parent_ids_,
                preproc_args,
                config=config,
                verbose=verbose,
                retry=retry_,
                retry_delay_min=retry_delay_min,
                retry_delay_max=retry_delay_max,
            )

        return rowid_list

    def _rectify_ids(self, parent_rowids):
        r"""
        Filters any rows containing None ids and transforms many-to-one sets of
        rowids into hashable UUIDS.

        Example:
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3()
            >>> depc.clear_all()
            >>> tablename = 'vocab'
            >>> tablename = 'indexer'
            >>> table = depc[tablename]
            >>> parent_rowids = [[1, 2, 3]]
            >>> rectify_tup = table._rectify_ids(parent_rowids)
            >>> parent_ids_, preproc_args, idxs1, idxs2 = rectify_tup
            ......
            >>> result = ('parent_ids_ = %r' % (parent_ids_,)) + '\n'
            >>> result += ('preproc_args = %r' % (preproc_args,))
            >>> print(result)

            parent_ids_ = [(UUID('356a192b-7913-b04c-5457-4d18c28d46e6'),)]
            preproc_args = [[1, 2, 3]]

        Example:
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import *  # NOQA
            >>> depc = testdata_depc3()
            >>> depc.clear_all()
            >>> tablename = 'vocab'
            >>> tablename = 'indexer'
            >>> table = depc[tablename]
            >>> parent_rowids = [[1, 2, 3]]
            >>> rowids = depc.get_rowids(tablename, parent_rowids)
            >>> model_uuid_list = table.get_internal_columns(rowids, ('model_uuid',))
            >>> model_uuid = model_uuid_list[0]
            >>> print('model_uuid = %r' % (model_uuid,))
            >>> rowids2 = table.get_model_rowids(model_uuid_list)
        """
        # Force entire row to be none if any are none
        anyNone_flags = [x is None or any(ut.flag_None_items(x)) for x in parent_rowids]
        idxs2 = ut.where(anyNone_flags)
        idxs1 = ut.index_complement(idxs2, len_=len(parent_rowids))
        valid_parent_ids_ = ut.take(parent_rowids, idxs1)

        preproc_args = valid_parent_ids_
        if self.ismulti:
            # Convert any parent-id containing multiple values into a hash of uuids
            multi_parent_flags = self.get_parent_col_attr('ismulti')
            num_parents = len(multi_parent_flags)
            multi_parent_colxs = ut.where(multi_parent_flags)
            normal_colxs = ut.index_complement(multi_parent_colxs, num_parents)
            multi_parents = [
                ut.apply_grouping(ids_, multi_parent_colxs) for ids_ in valid_parent_ids_
            ]
            normal_parents = [
                ut.apply_grouping(ids_, normal_colxs) for ids_ in valid_parent_ids_
            ]
            # TODO: give each table a uuid getter function that derives from
            # get_root_uuids
            multicol_tables = ut.take(self.parents(), multi_parent_colxs)
            parent_uuid_getters = [
                self.depc.get_root_uuid if col == self.depc.root else ut.identity
                for col in multicol_tables
            ]

            parent_uuids_list = [
                [
                    uuid_getter(ids_)
                    for uuid_getter, ids_ in zip(parent_uuid_getters, ids_tup)
                ]
                for ids_tup in multi_parents
            ]
            multiset_uuid_list = [
                [ut.hashable_to_uuid(uuids) for uuids in parent_uuids_tup]
                for parent_uuids_tup in parent_uuids_list
            ]
            # preproc args are usually the same as parent ids.  Model tables
            # are the exception.
            parent_ids_ = [
                tuple(
                    ut.ungroup(
                        [uuids, normalids],
                        [multi_parent_colxs, normal_colxs],
                        num_parents - 1,
                    )
                )
                for uuids, normalids in zip(multiset_uuid_list, normal_parents)
            ]
        else:
            parent_ids_ = valid_parent_ids_
        rectify_tup = parent_ids_, preproc_args, idxs1, idxs2
        return rectify_tup

    def _unrectify_ids(self, rowid_list_, parent_rowids, idxs1, idxs2):
        """
        Ensures that output is the same length as input. Inserts necessary
        Nones where the original input was also None.
        """
        # FIXME: turn into generator
        rowid_list = ut.ungroup([rowid_list_], [idxs1], len(parent_rowids) - 1)
        return rowid_list

    def get_rowid(
        self,
        parent_rowids,
        config=None,
        ensure=True,
        eager=True,
        nInput=None,
        recompute=False,
        _debug=None,
        num_retries=1,
    ):
        r"""
        Returns the rowids of derived properties.  If they do not exist it
        computes them.

        Args:
            parent_rowids (list): list of tuples with the parent rowids as the
                    value of each tuple
            config (None): (default = None)
            ensure (bool): eager evaluation if True (default = True)
            eager (bool): (default = True)
            nInput (int): (default = None)
            recompute (bool): (default = False)
            _debug (None): (default = None) deprecated; no-op

        Returns:
            list: rowid_list

        CommandLine:
            python -m dtool.depcache_table --exec-get_rowid

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache2 import testdata_depc3
            >>> depc = testdata_depc3()
            >>> table = depc['labeler']
            >>> exec(ut.execstr_funckw(table.get_rowid), globals())
            >>> config = table.configclass()
            >>> parent_rowids = list(zip([1, None, None, 2]))
            >>> rowids = table.get_rowid(parent_rowids, config=config)
            >>> result = ('rowids = %r' % (rowids,))
            >>> print(result)
            rowids = [1, None, None, 2]
        """
        logger.debug(
            '[deptbl.get_rowid] Get %s rowids via %d parent superkeys'
            % (self.tablename, len(parent_rowids))
        )
        logger.debug('[deptbl.get_rowid] config = %r' % (config,))
        logger.debug('[deptbl.get_rowid] ensure = %r' % (ensure,))

        # Ensure inputs are in the correct format / remove Nones
        # Collapse multi-inputs into a UUID hash
        rectify_tup = self._rectify_ids(parent_rowids)
        (parent_ids_, preproc_args, idxs1, idxs2) = rectify_tup
        # Do the getting / adding work
        if recompute:
            logger.info('REQUESTED RECOMPUTE')
            # get existing rowids, delete them, recompute the request
            rowid_list_ = self._get_rowid(
                parent_ids_,
                config=config,
                eager=True,
                nInput=None,
            )
            rowid_list_ = list(rowid_list_)
            needs_recompute_rowids = ut.filter_Nones(rowid_list_)
            try:
                self._recompute_and_store(needs_recompute_rowids)
            except Exception:
                # If the config changes, there is nothing we can do.
                # We have to delete the rows.
                self.delete_rows(rowid_list_)
        if ensure or recompute:
            # Compute properties if they do not exist
            for try_num in range(num_retries):
                try:
                    rowid_list_ = self.ensure_rows(
                        parent_ids_,
                        preproc_args,
                        config=config,
                    )
                except ExternalStorageException:
                    if try_num == num_retries - 1:
                        raise
        else:
            rowid_list_ = self._get_rowid(
                parent_ids_,
                config=config,
                eager=eager,
                nInput=nInput,
            )
        # Map outputs to correspond with inputs
        rowid_list = self._unrectify_ids(rowid_list_, parent_rowids, idxs1, idxs2)
        return rowid_list

    # @profile
    def _get_rowid(self, parent_ids_, config=None, eager=True, nInput=None):
        """
        Returns rowids using parent superkeys. Does not add non-existing
        properties.
        """
        colnames = (self.rowid_colname,)
        config_rowid = self.get_config_rowid(config=config)
        logger.debug('_get_rowid')
        logger.debug('_get_rowid self.tablename = %r ' % (self.tablename,))
        logger.debug('_get_rowid parent_ids_ = %s' % (ut.trunc_repr(parent_ids_)))
        logger.debug('_get_rowid config = %s' % (config))
        logger.debug('_get_rowid self.rowid_colname = %s' % (self.rowid_colname))
        logger.debug('_get_rowid config_rowid = %s' % (config_rowid))
        andwhere_colnames = self.superkey_colnames
        params_iter = (ids_ + (config_rowid,) for ids_ in parent_ids_)
        # TODO: make sure things that call this can accept a generator
        # Then remove this next line
        params_iter = list(params_iter)
        # logger.info('**params_iter = %r' % (params_iter,))
        rowid_list = self.db.get_where_eq(
            self.tablename,
            colnames,
            params_iter,
            andwhere_colnames,
            eager=eager,
            nInput=nInput,
        )
        logger.debug('_get_rowid rowid_list = %s' % (ut.trunc_repr(rowid_list)))
        return rowid_list

    def clear_table(self):
        """
        Deletes all data in this table
        """
        # TODO: need to clear one-to-one dependencies as well
        logger.info('Clearing data in %r' % (self,))
        self.db.drop_table(self.tablename)
        self.db.add_table(**self._get_addtable_kw())

    # @profile
    def delete_rows(self, rowid_list, delete_extern=None, dry=False, verbose=None):
        """
        CommandLine:
            python -m dtool.depcache_table --exec-delete_rows

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache import testdata_depc
            >>> depc = testdata_depc()
            >>> #table = depc['keypoint']
            >>> table = depc['chip']
            >>> exec(ut.execstr_funckw(table.delete_rows), globals())
            >>> tablename = table.tablename
            >>> graph = depc.explicit_graph
            >>> config1 = None
            >>> config2 = table.configclass(version=-1)
            >>> config3 = table.configclass(version=-1, ext='.jpg')
            >>> config4 = table.configclass(ext='.jpg')
            >>> # Create several configs of rowid
            >>> aids = [1, 2, 3]
            >>> depc.get_rowids('spam', aids, config=config1)
            >>> depc.get_rowids('spam', aids, config=config2)
            >>> depc.get_rowids('spam', aids, config=config3)
            >>> depc.get_rowids('spam', aids, config=config4)
            >>> # Delete the png configs
            >>> rowid_list1 = depc.get_rowids(table.tablename, aids,
            >>>                               config=config2)
            >>> rowid_list2 = depc.get_rowids(table.tablename, aids,
            >>>                               config=config1)
            >>> rowid_list = rowid_list1 + rowid_list2
            >>> assert len(ut.setintersect_ordered(rowid_list1, rowid_list2)) == 0
            >>> table.delete_rows(rowid_list)
        """
        # import networkx as nx
        # from wbia.dtool.algo.preproc import preproc_feat
        if self.on_delete is not None and not dry:
            self.on_delete()
        if delete_extern is None:
            delete_extern = self.rm_extern_on_delete
        if verbose is None:
            verbose = False
        if ut.NOT_QUIET:
            if ut.VERBOSE:
                logger.info(
                    'Requested delete of %d rows from %s'
                    % (len(rowid_list), self.tablename)
                )
                if dry:
                    logger.info('Dry run')
            # logger.info('delete_extern = %r' % (delete_extern,))
        depc = self.depc

        # TODO:
        # REMOVE EXTERNAL FILES
        internal_colnames = self.get_intern_data_col_attr('intern_colname')
        is_extern = self.get_intern_data_col_attr('is_external_pointer')
        extern_colnames = tuple(ut.compress(internal_colnames, is_extern))
        if len(extern_colnames) > 0:
            uris = self.get_internal_columns(
                rowid_list,
                extern_colnames,
                unpack_scalars=False,
                eager=True,
                keepwrap=False,
            )
            absuris = []
            for uri in it.chain.from_iterable(uris):
                if not isinstance(uri, tuple):
                    uri = [uri]
                for uri_ in uri:
                    absuris.append(join(self.extern_dpath, uri_))
            fpaths = [fpath for fpath in absuris if exists(fpath)]
            if delete_extern:
                if ut.VERBOSE or len(fpaths) > 0:
                    logger.info('deleting {} existing internal files'.format(len(fpaths)))
                if not dry:
                    ut.remove_fpaths(fpaths, verbose=verbose)
            else:
                if ut.VERBOSE or len(fpaths) > 0:
                    logger.info('Leaving {} dangling filepaths'.format(len(fpaths)))

        # DELETE EXPLICITLY DEFINED CHILDREN
        # (TODO: handle implicit definitions)
        if True:

            def get_child_partial_rowids(child_table, rowid_list, parent_colnames):
                colnames = (child_table.rowid_colname,)
                andwhere_colnames = parent_colnames
                params_iter = ((rowid,) for rowid in rowid_list)
                params_iter = list(params_iter)
                child_db = depc[child_table.tablename].db
                child_unflat_rowids = child_db.get_where_eq(
                    child_table.tablename,
                    colnames,
                    params_iter,
                    andwhere_colnames,
                    unpack_scalars=False,
                    keepwrap=False,
                )
                child_rowids = ut.flatten(child_unflat_rowids)
                return child_rowids

            if ut.VERBOSE:
                if self.children:
                    logger.info('Deleting from %r children' % (len(self.children),))
                else:
                    logger.info('Table is a leaf node')

            for child in self.children:
                child_table = self.depc[child]
                if not child_table.ismulti:
                    # Hack, wont work for vsone / multisets
                    parent_colnames = (
                        child_table.parent[self.tablename]['intern_colname'],
                    )
                    child_rowids = get_child_partial_rowids(
                        child_table, rowid_list, parent_colnames
                    )
                    child_table.delete_rows(child_rowids, dry=dry)

        if ut.NOT_QUIET:
            non_none_rowids = ut.filter_Nones(rowid_list)
            if ut.VERBOSE or len(non_none_rowids) > 0:
                logger.info(
                    'Deleting %d non-None rows from %s'
                    % (len(non_none_rowids), self.tablename)
                )
                logger.info('...done!')

        # Finalize: Delete rows from this table
        if not dry:
            self.db.delete_rowids(self.tablename, rowid_list)
            num_deleted = len(ut.filter_Nones(rowid_list))
        else:
            num_deleted = 0
        return num_deleted

    def _resolve_requested_columns(self, requested_colnames):
        ########
        # Map requested colnames flat to internal colnames
        ########
        # Get requested column information
        requestable_col_attrs = self.requestable_col_attrs()
        requested_colattrs = ut.take(requestable_col_attrs, requested_colnames)
        # Make column indicies iterable for grouping
        intern_colxs = [
            xs if ut.isiterable(xs) else [xs]
            for xs in ut.take_column(requested_colattrs, 'intern_colx')
        ]
        nested_offsets_end = ut.cumsum(ut.lmap(len, intern_colxs))
        nested_offsets_start = [0] + nested_offsets_end[:-1]
        # Mark any columns with external information
        isextern_flags = ut.dict_take_column(requested_colattrs, 'is_extern', False)
        extern_colattrs = ut.compress(requested_colattrs, isextern_flags)
        extern_resolve_colxs = ut.compress(nested_offsets_start, isextern_flags)
        extern_read_funcs = ut.take_column(extern_colattrs, 'read_func')
        intern_colnames_ = ut.take_column(self.internal_col_attrs, 'intern_colname')
        intern_colnames = ut.unflat_take(intern_colnames_, intern_colxs)

        # TODO: this can be cleaned up
        nesting_xs = [
            x1 if x2 - x1 == 1 else list(range(x1, x2))
            for x1, x2 in zip(nested_offsets_start, nested_offsets_end)
        ]
        extern_resolve_tups = list(zip(extern_resolve_colxs, extern_read_funcs))
        flat_intern_colnames = tuple(ut.flatten(intern_colnames))
        return nesting_xs, extern_resolve_tups, flat_intern_colnames

    # @profile
    def get_row_data(
        self,
        tbl_rowids,
        colnames=None,
        _debug=None,
        read_extern=True,
        num_retries=1,
        eager=True,
        nInput=None,
        ensure=True,
        delete_on_fail=True,
        showprog=False,
        unpack_columns=None,
    ):
        r"""
        FIXME: unpacking is confusing with sql controller
        TODO: Clean up and allow for eager=False

        colnames = ('mask', 'size')

        CommandLine:
            python -m dtool.depcache_table --test-get_row_data:0
            python -m dtool.depcache_table --test-get_row_data:1

        Example:
            >>> # ENABLE_DOCTEST
            >>> from wbia.dtool.depcache_table import *  # NOQA
            >>> from wbia.dtool.example_depcache import testdata_depc
            >>> depc = testdata_depc()
            >>> table = depc['chip']
            >>> exec(ut.execstr_funckw(table.get_row_data), globals())
            >>> tbl_rowids = depc.get_rowids('chip', [1, 2, 3], _debug=True, recompute=True)
            >>> colnames = ('size_1', 'size', 'chip' + EXTERN_SUFFIX, 'chip')
            >>> kwargs = dict(read_extern=True, num_retries=1, _debug=True)
            >>> prop_list = table.get_row_data(tbl_rowids, colnames, **kwargs)
            >>> prop_list0 = ut.take_column(prop_list, [0, 1, 2]) # data subset
            >>> result = (ut.repr2(prop_list0, nl=1))
            >>> print(result)
            >>> #_debug, num_retries, read_extern = True, 1, True
            >>> prop_gen = table.get_row_data(tbl_rowids, colnames, eager=False)
            >>> prop_list2 = list(prop_gen)
            >>> assert len(prop_list2) == len(prop_list), 'inconsistent lens'
            >>> assert all([ut.lists_eq(prop_list2[1], prop_list[1]) for x in range(len(prop_list))]), 'inconsistent vals'
            >>> chips = table.get_row_data(tbl_rowids, 'chip', eager=False)

            [
                [2453, (1707, 2453), 'chip_chip_id=1_pyrappzicqoskdjq.png'],
                [250, (300, 250), 'chip_chip_id=2_pyrappzicqoskdjq.png'],
                [372, (545, 372), 'chip_chip_id=3_pyrappzicqoskdjq.png'],
            ]


        Example:
            >>> # ENABLE_DOCTEST
            >>> # Test external / ensure getters
            >>> from wbia.dtool.example_depcache import *  # NOQA
            >>> depc = testdata_depc()
            >>> table = depc['chip']
            >>> exec(ut.execstr_funckw(table.get_row_data), globals())
            >>> depc.clear_all()
            >>> config = {}
            >>> aids = [1,]
            >>> read_extern = False
            >>> tbl_rowids = depc.get_rowids('chip', aids, config=config)
            >>> data_fpaths = depc.get('chip', aids, 'chip', config=config, read_extern=False)
            >>> # Ensure data is recomputed if an external file is missing
            >>> ut.remove_fpaths(data_fpaths)
            >>> data = table.get_row_data(tbl_rowids, 'chip', read_extern=False, ensure=False)
            >>> data = table.get_row_data(tbl_rowids, 'chip', read_extern=False, ensure=True)
        """
        logger.debug(
            ('Get col of tablename=%r, colnames=%r with ' 'tbl_rowids=%s')
            % (self.tablename, colnames, ut.trunc_repr(tbl_rowids))
        )
        ####
        # Resolve requested column names
        if unpack_columns is None:
            unpack_columns = self.default_to_unpack
        if colnames is None:
            requested_colnames = self.data_colnames
        elif isinstance(colnames, str):
            # Unpack columns if only a single column is requested.
            requested_colnames = (colnames,)
            unpack_columns = True
        else:
            requested_colnames = colnames

        logger.debug('requested_colnames = %r' % (requested_colnames,))
        tup = self._resolve_requested_columns(requested_colnames)
        nesting_xs, extern_resolve_tups, flat_intern_colnames = tup

        logger.debug(
            '[deptbl.get_row_data] flat_intern_colnames = %r' % (flat_intern_colnames,)
        )

        nonNone_flags = ut.flag_not_None_items(tbl_rowids)
        nonNone_tbl_rowids = ut.compress(tbl_rowids, nonNone_flags)
        idxs1 = ut.where(nonNone_flags)

        idxs2 = ut.index_complement(idxs1, len(tbl_rowids))

        if nInput is None and ut.is_listlike(nonNone_tbl_rowids):
            nInput = len(nonNone_tbl_rowids)

        generator_version = not eager

        raw_prop_list = self.get_internal_columns(
            nonNone_tbl_rowids,
            flat_intern_colnames,
            eager=eager,
            nInput=nInput,
            unpack_scalars=True,
            keepwrap=True,
            showprog=showprog,
        )

        def tup_unflat_take(items_list, unflat_index_list):
            r"""
            Hack for depcache, that needs a tuple version of ut.unflat_take
            """

            def tuptake(list_, index_list):
                try:
                    return tuple([list_[index] for index in index_list])
                except TypeError:
                    return list_[index_list]

            return tuple(
                [
                    tup_unflat_take(items_list, xs)
                    if isinstance(xs, list)
                    else tuptake(items_list, xs)
                    for xs in unflat_index_list
                ]
            )

        # if len(raw_prop_list) > 0:
        if nInput > 0 and len(nonNone_tbl_rowids) > 0:
            if generator_version:

                def _generator_resolve_all():
                    extern_dpath = self.extern_dpath
                    for rawprop in raw_prop_list:
                        if rawprop is None:
                            raise Exception(
                                'raw prop was None, but it should always be a tuple. '
                                'This may indicate that the cache needs to be cleared'
                            )

                        exprop = list(rawprop)
                        # Modify prop with external data
                        for extern_colx, read_func in extern_resolve_tups:
                            uri = exprop[extern_colx]
                            uri_full = join(extern_dpath, uri)
                            if read_extern:
                                data = read_func(uri_full)
                            else:
                                data = uri_full
                                if ensure:
                                    ut.assertpath(uri_full)
                            exprop[extern_colx] = data
                        # nestprop = ut.unflat_take(exprop, nesting_xs)
                        nestprop = tup_unflat_take(exprop, nesting_xs)
                        yield nestprop

                prop_gen = _generator_resolve_all()
                if unpack_columns:
                    prop_gen = (None if p is None else p[0] for p in prop_gen)
                assert len(idxs2) == 0, 'noneager mode not fully worked out yet'
                return prop_gen
            else:
                # logger.info('raw_prop_list = %r' % (raw_prop_list,))
                if num_retries > 0:
                    raw_prop_list = list(raw_prop_list)  # TODO tee iterator instead?
                for try_num in range(num_retries + 1):
                    tries_left = num_retries - try_num
                    try:
                        prop_listT = self._resolve_any_external_data(
                            nonNone_tbl_rowids,
                            raw_prop_list,
                            extern_resolve_tups,
                            ensure,
                            read_extern,
                            delete_on_fail,
                            tries_left,
                        )
                    except ExternalStorageException:
                        if tries_left == 0:
                            raise
                    else:
                        # Things worked, dont need to try again
                        break
                ####
                # Unflatten data into any given nested structure
                if len(prop_listT) > 0:
                    nested_proplistT = ut.unflat_take(prop_listT, nesting_xs)
                    for tx in ut.where([isinstance(xs, list) for xs in nesting_xs]):
                        nested_proplistT[tx] = list(zip(*nested_proplistT[tx]))
                    prop_list = list(zip(*nested_proplistT))
                else:
                    prop_list = []
                ####
                # Unpack single column datas if requested
                if unpack_columns:
                    prop_list = [None if p is None else p[0] for p in prop_list]
        else:
            prop_list = []

        if len(idxs2) > 0:
            prop_list = ut.ungroup(
                [prop_list, [None] * len(idxs2)], [idxs1, idxs2], len(tbl_rowids) - 1
            )
        return prop_list

    def _resolve_any_external_data(
        self,
        nonNone_tbl_rowids,
        raw_prop_list,
        extern_resolve_tups,
        ensure,
        read_extern,
        delete_on_fail,
        tries_left,
    ):
        ####
        # Read data specified by any external columns
        extern_dpath = self.extern_dpath
        try:
            prop_listT = list(zip(*raw_prop_list))
        except TypeError as ex:
            ut.printex(ex, 'error on prop_list shape', keys=['raw_prop_list'])
            raise

        for extern_colx, read_func in extern_resolve_tups:
            logger.debug('[deptbl.get_row_data] read_func = %r' % (read_func,))
            data_list = []
            failed_list = []
            for uri in prop_listT[extern_colx]:
                uri_full = join(extern_dpath, uri)
                try:
                    if read_extern:
                        data = read_func(uri_full)
                    else:
                        if ensure:
                            ut.assertpath(uri_full)
                        data = uri_full
                except Exception as ex:
                    ut.printex(
                        ex,
                        'failed to load external data',
                        iswarning=(tries_left > 0),
                        keys=[
                            'tries_left',
                            'uri',
                            'uri_full',
                            (exists, 'uri_full'),
                            'read_func',
                        ],
                    )
                    if tries_left == 0:
                        raise
                    failed_list.append(True)
                    data = None
                else:
                    failed_list.append(False)
                data_list.append(data)
            if any(failed_list):
                # FIXME: should directly recompute the data in the rows
                # rather than deleting the rowids.  Need the parent ids and
                # config to do that.
                failed_uris = ut.compress(prop_listT[extern_colx], failed_list)
                logger.info(
                    'Failed to read %s' % (ut.trunc_repr(failed_uris, maxlen=300))
                )
                failed_rowids = ut.compress(nonNone_tbl_rowids, failed_list)
                if delete_on_fail:
                    self._recompute_external_storage(failed_rowids)
                    # self.delete_rows(failed_rowids, delete_extern=None)
                raise ExternalStorageException(
                    'Some cached filenames failed to read. '
                    'Need to recompute %d/%d rows' % (sum(failed_list), len(failed_list))
                )
                # raise Exception('Non existant data on disk. Need to recompute rows')
            prop_listT[extern_colx] = data_list
        return prop_listT

    def _recompute_external_storage(self, tbl_rowids):
        """
        Recomputes the external file stored for this row.
        This DOES NOT modify the depcache internals.
        """
        logger.info('Recomputing external data (_recompute_external_storage)')
        # TODO: need to rectify parent ids?

        parent_rowids = self.get_parent_rowids(tbl_rowids)
        parent_rowargs = self.get_parent_rowargs(tbl_rowids)

        # configs = self.get_row_configs(tbl_rowids)
        # assert ut.allsame(list(map(id, configs))), 'more than one config not yet supported'
        # TODO; groupby config
        config_rowids = self.get_row_cfgid(tbl_rowids)
        unique_cfgids, groupxs = ut.group_indices(config_rowids)

        for xs, cfgid in zip(groupxs, unique_cfgids):
            parent_ids = ut.take(parent_rowids, xs)
            parent_args = ut.take(parent_rowargs, xs)
            config = self.get_config_from_rowid([cfgid])[0]
            dirty_params_iter = self._compute_dirty_rows(
                parent_ids, parent_args, config_rowid=cfgid, config=config
            )
            # Evaulate just to ensure storage
            for _ in dirty_params_iter:
                pass

    def _recompute_and_store(self, tbl_rowids, config=None):
        """
        Recomputes all data stored for this row.
        This DOES modify the depcache internals.
        """
        logger.info('Recomputing external data (_recompute_and_store)')
        if len(tbl_rowids) == 0:
            return
        parent_rowids = self.get_parent_rowids(tbl_rowids)
        parent_rowargs = self.get_parent_rowargs(tbl_rowids)
        # configs = self.get_row_configs(tbl_rowids)
        # assert ut.allsame(list(map(id, configs))), 'more than one config not yet supported'
        # TODO; groupby config

        if config is None:
            config_rowids = self.get_row_cfgid(tbl_rowids)
            unique_cfgids, groupxs = ut.group_indices(config_rowids)
        else:
            # This is incredibly hacky.
            pass

        colnames = self.computable_colnames()

        for xs, cfgid in zip(groupxs, unique_cfgids):
            parent_ids = ut.take(parent_rowids, xs)
            parent_args = ut.take(parent_rowargs, xs)
            rowids = ut.take(tbl_rowids, xs)
            config = self.get_config_from_rowid([cfgid])[0]
            dirty_params_iter = self._compute_dirty_rows(
                parent_ids, parent_args, config_rowid=cfgid, config=config
            )
            # Evaulate to external and internal storage
            self.db.set(self.tablename, colnames, dirty_params_iter, rowids)

    # togroup_args = [parent_rowids]
    # grouped_parent_ids = ut.apply_grouping(parent_rowids, groupxs)
    # unique_args_list = [unique_configs]

    # raw_prop_lists = []
    # # func = ut.partial(self.preproc_func, self.depc)
    # def groupmap_func(group_args, unique_args):
    #    config_ = unique_args[0]
    #    argsT = group_args
    #    propgen = self.preproc_func(self.depc, *argsT, config=config_)
    #    return list(propgen)

    # def grouped_map(groupmap_func, groupxs, togroup_args, unique_args_list):
    #    # TODO; genralize to utool
    #    grouped_args_list = [ut.apply_grouping(togroup, groupxs) for
    #                         togroup in togroup_args]
    #    group_ret_list = []
    #    for group_args, unique_args in zip(grouped_args_list,
    #                                         unique_args_list):
    #        group_ret = groupmap_func(group_args, unique_args)
    #        group_ret_list.append(group_ret)
    #    ret_list = ut.ungroup(group_ret_list, groupxs)
    #    return ret_list
    #
    # raw_prop_list = grouped_map(groupmap_func, groupxs, togroup_args,
    #                            unique_args_list)

    # @profile
    def get_internal_columns(
        self,
        tbl_rowids,
        colnames=None,
        eager=True,
        nInput=None,
        unpack_scalars=True,
        keepwrap=False,
        showprog=False,
    ):
        """
        Access data in this table using the table PRIMARY KEY rowids (not
        depc PRIMARY ids)
        """
        prop_list = self.db.get(
            self.tablename,
            colnames,
            tbl_rowids,
            id_colname=self.rowid_colname,
            eager=eager,
            nInput=nInput,
            unpack_scalars=unpack_scalars,
            keepwrap=keepwrap,
            showprog=showprog,
        )
        return prop_list

    def export_rows(self, rowid, target):
        """
        The goal of this is to export taggable data that can be used
        independantly of its dependant features.

        TODO List:
            * Gather information about columns
                * Native and (localized) external data
                    - <table>_rowid - non-transferable
                    - Parent UUIDS - non-transferable
                    - config rowid - non-transferable
                    - model_uuid -
                    - augment_bit - transferable - trivial
                    - words_extern_uri - copy to destination
                    - feat_setsize - transferable - trivial
                    - model_tag

                * Should also gather info from manifest:
                    * feat_setuuid_primary_ids - non-transferable
                    * feat_setuuid_model_input - non-transferable

                * Should gather exhaustive config history

            * Save to disk

            * Add function to reload data in exported format

            * Getters should be able to specify a tag inplace of the root input
            for the tagged. Additionally native root-ids should also be
            allowed.


        rowid = 1
        """
        raise NotImplementedError('unfinished')
        colnames = tuple(self.db.get_column_names(self.tablename))
        colvals = self.db.get(self.tablename, colnames, [rowid])[0]  # NOQA

        uuid = self.get_model_uuid([rowid])[0]
        manifest_data = self.get_model_inputs(uuid)  # NOQA

        config_history = self.get_config_history([rowid])  # NOQA

        self.parent_col_attrs = self._infer_parentcol()
        self.data_col_attrs
        self.internal_col_attrs

        self.db.cur.execute('SELECT * FROM {tablename} WHERE rowid=?')
        pass

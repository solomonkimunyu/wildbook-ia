# -*- coding: utf-8 -*-
# Autogenerated on 09:16:59 2017/04/05
# flake8: noqa
import logging
import utool

print, rrr, profile = utool.inject2(__name__)
logger = logging.getLogger('wbia')


def reassign_submodule_attributes(verbose=1):
    """
    Updates attributes in the __init__ modules with updated attributes
    in the submodules.
    """
    import sys

    if verbose and '--quiet' not in sys.argv:
        print('dev reimport')
    # Self import
    import wbia.algo.graph

    # Implicit reassignment.
    seen_ = set([])
    for tup in IMPORT_TUPLES:
        if len(tup) > 2 and tup[2]:
            continue  # dont import package names
        submodname, fromimports = tup[0:2]
        submod = getattr(wbia.algo.graph, submodname)
        for attr in dir(submod):
            if attr.startswith('_'):
                continue
            if attr in seen_:
                # This just holds off bad behavior
                # but it does mimic normal util_import behavior
                # which is good
                continue
            seen_.add(attr)
            setattr(wbia.algo.graph, attr, getattr(submod, attr))


def reload_subs(verbose=1):
    """Reloads wbia.algo.graph and submodules"""
    if verbose:
        print('Reloading wbia.algo.graph submodules')
    rrr(verbose > 1)

    def wrap_fbrrr(mod):
        def fbrrr(*args, **kwargs):
            """fallback reload"""
            if verbose > 0:
                print('Auto-reload (using rrr) not setup for mod=%r' % (mod,))

        return fbrrr

    def get_rrr(mod):
        if hasattr(mod, 'rrr'):
            return mod.rrr
        else:
            return wrap_fbrrr(mod)

    def get_reload_subs(mod):
        return getattr(mod, 'reload_subs', wrap_fbrrr(mod))

    rrr(verbose > 1)
    try:
        # hackish way of propogating up the new reloaded submodule attributes
        reassign_submodule_attributes(verbose=verbose)
    except Exception as ex:
        print(ex)


rrrr = reload_subs

IMPORT_TUPLES = []
"""
Regen Command:
    cd /home/joncrall/code/wbia/wbia/algo/graph
    makeinit.py --modname=wbia.algo.graph
"""

# Copyright (C) 2021-2023 Modin authors
#
# SPDX-License-Identifier: Apache-2.0

"""Common classes and utilities."""

import logging
import inspect

from unidist.core.backends.common.data_id import DataID, is_data_id


class Operation:
    """
    Class that describes supported operations.

    Attributes
    ----------
    EXECUTE : int, default 1
        Execute remote task.
    GET : int, default 2
        Return local data to a requester.
    PUT_DATA : int, default 3
        Save the data to a local storage.
    PUT_OWNER : int, default 4
        Save the data location to a local storage.
    WAIT : int, default 5
        Return readiness signal of a local data to a requester.
    ACTOR_CREATE : int, default 6
        Create local actor instance.
    ACTOR_EXECUTE : int, default 7
        Execute method of a local actor instance.
    CLEANUP : int, default 8
        Cleanup local object storage for out-of-scope IDs.
    TASK_DONE : int, default 9
        Increment global task counter.
    GET_TASK_COUNT : int, default 10
        Exit event loop.
    CANCEL : int, default 11
        Return global task counter to a requester.
    """

    ### --- Master/worker operations --- ###
    EXECUTE = 1
    GET = 2
    PUT_DATA = 3
    PUT_OWNER = 4
    WAIT = 5
    ACTOR_CREATE = 6
    ACTOR_EXECUTE = 7
    CLEANUP = 8
    ### --- Monitor operations --- ###
    TASK_DONE = 9
    GET_TASK_COUNT = 10
    ### --- Common operations --- ###
    CANCEL = 11


default_class_properties = dir(type("dummy", (object,), {}))
# Mapping between operations and their names (e.g., Operation.EXECUTE: "EXECUTE")
operations_dict = dict(
    (value, name)
    for name, value in inspect.getmembers(Operation)
    if name not in default_class_properties
)


def get_op_name(op):
    """
    Get string operation name.

    Parameters
    ----------
    op : unidist.core.backends.mpi.core.common.Operation
        Operation type.

    Returns
    -------
    str
        String operation name.

    Raises
    ------
    KeyError
        If the operation does not match either of `operations_dict`.
    """
    op_name = operations_dict.get(op, None)
    if op_name is None:
        raise KeyError(f"Got unsupported operation `{op}`")
    return op_name


class MasterDataID(DataID):
    """
    Class for tracking data IDs of the main process.

    Class extends ``unidist.core.backends.common.data_id.DataID`` functionality with a garbage collection.

    Parameters
    ----------
    id_value : int
        An integer value, generated by executor process.
    garbage_collector : unidist.core.backends.mpi.core.executor.GarbageCollector
        A reference to the garbage collector instance.
    """

    def __init__(self, id_value, garbage_collector):
        super().__init__(id_value)
        self._gc = garbage_collector if garbage_collector else None

    def __del__(self):
        """Track object deletion by garbage collector."""
        # We check for existence of `_qc` attribute because
        # it might be deleted during serialization via `__getstate__`
        if hasattr(self, "_gc") and self._gc is not None:
            self._gc.collect(self.base_data_id())

    def __getstate__(self):
        """Remove a reference to garbage collector for correct `pickle` serialization."""
        attributes = self.__dict__.copy()
        del attributes["_gc"]
        return attributes

    def base_data_id(self):
        """
        Return the base class instance without garbage collector reference.

        Returns
        -------
        unidist.core.backends.common.data_id.DataID
            Base ``DataID`` class object without garbage collector reference.
        """
        return DataID(self._id)


def get_logger(logger_name, file_name, activate=False):
    """
    Configure logger and get it's instance.

    Parameters
    ----------
    logger_name : str
        Name of a logger.
    file_name : str
        File name.
    activate : bool
        Write logs or not.

    Returns
    -------
    object
        A Python logger object.
    """
    logger = logging.getLogger(logger_name)
    if not logger.hasHandlers():
        f_format = logging.Formatter("%(message)s")
        f_handler = logging.FileHandler(file_name, delay=True)
        f_handler.setFormatter(f_format)
        logger.addHandler(f_handler)

    if activate:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.NOTSET)

    return logger


def unwrapped_data_ids_list(args):
    """
    Transform all found data ID objects to their underlying value in an iterable object.

    Parameters
    ----------
    args : iterable
        Sequence of data.

    Returns
    -------
    list
        Transformed list with underlying ``DataID`` values.
    """
    if args is None:
        return [None]
    elif is_data_id(args):
        return [args._id]
    else:
        return [arg._id if is_data_id(arg) else None for arg in args]


def master_data_ids_to_base(o_ids):
    """
    Transform all data ID objects of the main process to its base class instances.

    Cast ``unidist.core.backends.mpi.core.common.MasterDataID`` to it's base ``unidist.backend.common.data_id.DataID`` class
    to remove a reference to garbage collector.

    Parameters
    ----------
    o_ids : iterable
        Sequence of ``unidist.core.backends.mpi.core.common.MasterDataID`` objects.

    Returns
    -------
    list
        Transformed list.
    """
    if o_ids is None:
        return None
    elif is_data_id(o_ids):
        return o_ids.base_data_id()
    id_list = [o_id.base_data_id() for o_id in o_ids]
    return id_list


def unwrap_data_ids(data_ids):
    """
    Find all data ID instances of the main process and prepare for communication with worker process.

    Call `base_data_id` on each instance.

    Parameters
    ----------
    data_ids : iterable
        Iterable objects to transform recursively.

    Returns
    -------
    iterable
        Transformed iterable object (task arguments).
    """
    if type(data_ids) in (list, tuple, dict):
        container = type(data_ids)()
        for value in data_ids:
            unwrapped_value = unwrap_data_ids(
                data_ids[value] if isinstance(data_ids, dict) else value
            )
            if isinstance(container, list):
                container += [unwrapped_value]
            elif isinstance(container, tuple):
                container += (unwrapped_value,)
            elif isinstance(container, dict):
                container.update({value: unwrapped_value})
        return container
    else:
        return data_ids.base_data_id() if is_data_id(data_ids) else data_ids


def materialize_data_ids(data_ids, unwrap_data_id_impl, is_pending=False):
    """
    Traverse iterable object and materialize all data IDs.

    Find all ``unidist.core.backends.common.data_id.DataID`` instances and call `unwrap_data_id_impl` on them.

    Parameters
    ----------
    data_ids : iterable
        Iterable objects to transform recursively.
    unwrap_data_id_impl : callable
        Function to get the ID associated data from the local object store if available.
    is_pending : bool, default: False
        Status of data materialization attempt as a flag.

    Returns
    -------
    iterable or bool
        Transformed iterable object (task arguments) and status if all ``DataID`` instances were transformed.
    """

    def _unwrap_data_id(*args):
        nonlocal is_pending
        value, progress = unwrap_data_id_impl(*args)
        if not is_pending:
            is_pending = progress
        return value

    if type(data_ids) in (list, tuple, dict):
        container = type(data_ids)()
        for value in data_ids:
            unwrapped_value, is_pending = materialize_data_ids(
                data_ids[value] if isinstance(data_ids, dict) else value,
                unwrap_data_id_impl,
                is_pending=is_pending,
            )
            if isinstance(container, list):
                container += [_unwrap_data_id(unwrapped_value)]
            elif isinstance(container, tuple):
                container += (_unwrap_data_id(unwrapped_value),)
            elif isinstance(container, dict):
                container.update({value: _unwrap_data_id(unwrapped_value)})
        return container, is_pending
    else:
        unwrapped = _unwrap_data_id(data_ids)
        return unwrapped, is_pending

from collections import defaultdict

import unidist.core.backends.mpi.core.common as common
import unidist.core.backends.mpi.core.communication as communication
from unidist.core.backends.mpi.core.async_operations import AsyncOperations
from unidist.core.backends.mpi.core.worker.object_store import ObjectStore


mpi_state = communication.MPIState.get_instance()
# Logger configuration
# When building documentation we do not have MPI initialized so
# we use the condition to set "worker_0.log" in order to build it succesfully.
logger_name = "worker_{}".format(mpi_state.rank if mpi_state is not None else 0)
log_file = "{}.log".format(logger_name)
logger = common.get_logger(logger_name, log_file)


class RequestStore:
    """
    Class that stores data requests that couldn't be satisfied now.

    Notes
    -----
    Supports GET and WAIT requests.
    """

    __instance = None

    REQ_DATA = 0
    REQ_WAIT = 1
    REQ_DATA_CACHE = 2

    def __init__(self):
        # Data requests {DataId : [ Set of Ranks ]}
        self._data_request = defaultdict(set)
        # Blocking data requests {DataId : [ Set of Ranks ]}
        self._blocking_data_request = defaultdict(set)
        # Wait requests {DataId : Rank}
        self._wait_request = {}
        # Cache for already requested ids
        self._data_request_cache = set()

    @classmethod
    def get_instance(cls):
        """
        Get instance of ``RequestStore``.

        Returns
        -------
        RequestStore
        """
        if cls.__instance is None:
            cls.__instance = RequestStore()
        return cls.__instance

    def put(self, data_id, rank, request_type, is_blocking_op=False):
        """
        Save request type for this data ID for later processing.

        Parameters
        ----------
        data_id : unidist.core.backends.common.data_id.DataID
            An ID to data.
        rank : int
            Source rank requester.
        request_type : int
            Type of request.
        is_blocking_op : bool
            Whether the get request should be blocking or not.
            If ``True``, the request should be processed immediatly
            even for a worker since it can get into controller mode.
        """
        if request_type == self.REQ_DATA:
            if is_blocking_op:
                self._blocking_data_request[data_id].add(rank)
            else:
                self._data_request[data_id].add(rank)
        elif request_type == self.REQ_WAIT:
            self._wait_request[data_id] = rank
        elif request_type == self.REQ_DATA_CACHE:
            self._data_request_cache.add(data_id)
        else:
            raise ValueError("Unsupported request type option!")

    def is_already_requested(self, data_id):
        """
        Check if particular `data_id` was already requested from another MPI process.

        Parameters
        ----------
        data_id : unidist.core.backends.common.data_id.DataID
            An ID to data.

        Returns
        -------
        bool
            ``True`` if communnication request was happened for this ID.
        """
        return data_id in self._data_request_cache

    def clear_cache(self, data_id):
        """
        Clear internal cache for happened communication requests.

        Parameters
        ----------
        data_id : unidist.core.backends.common.data_id.DataID
            An ID to data.
        """
        self._data_request_cache.discard(data_id)

    def check_pending_get_requests(self, data_ids):
        """
        Check if `GET` event on this `data_ids` is waiting to be processed.

        Process the request if data ID available in local object store.

        Parameters
        ----------
        data_id : iterable or unidist.core.backends.common.data_id.DataID
            An ID or list of IDs to data.
        """

        def check_request(data_id):
            # Check non-blocking data requests for one of the workers
            if data_id in self._data_request:
                ranks_with_get_request = self._data_request[data_id]
                for rank_num in ranks_with_get_request:
                    # Data is already in DataMap, so not problem here
                    self.process_get_request(rank_num, data_id, is_blocking_op=False)
                del self._data_request[data_id]
            # Check blocking data requests for other of the workers
            if data_id in self._blocking_data_request:
                ranks_with_get_request = self._blocking_data_request[data_id]
                for rank_num in ranks_with_get_request:
                    # Data is already in DataMap, so not problem here
                    self.process_get_request(rank_num, data_id, is_blocking_op=True)
                del self._blocking_data_request[data_id]

        if isinstance(data_ids, (list, tuple)):
            for data_id in data_ids:
                check_request(data_id)
        else:
            check_request(data_ids)

    def check_pending_wait_requests(self, data_ids):
        """
        Check if `WAIT` event on this `data_ids` is waiting to be processed.

        Process the request if data ID available in local object store.
        Send signal without any data.

        Parameters
        ----------
        data_id : iterable or unidist.core.backends.common.data_id.DataID
            An ID or list of IDs to data.
        """
        if isinstance(data_ids, (list, tuple)):
            for data_id in data_ids:
                if data_id in self._wait_request:
                    # Data is already in DataMap, so not problem here.
                    # We use a blocking send here because the receiver is waiting for the result.
                    communication.mpi_send_object(
                        communication.MPIState.get_instance().comm,
                        data_id,
                        communication.MPIRank.ROOT,
                    )
                    del self._wait_request[data_id]
        else:
            if data_ids in self._wait_request:
                # We use a blocking send here because the receiver is waiting for the result.
                communication.mpi_send_object(
                    communication.MPIState.get_instance().comm,
                    data_ids,
                    communication.MPIRank.ROOT,
                )
                del self._wait_request[data_ids]

    def process_wait_request(self, data_id):
        """
        Satisfy WAIT operation request from another process.

        Save request for later processing if `data_id` is not available currently.

        Parameters
        ----------
        data_id : unidist.core.backends.common.data_id.DataID
            Chech if `data_id` is available in object store.

        Notes
        -----
        Only ROOT rank is supported for now, therefore no rank argument needed.
        """
        if ObjectStore.get_instance().contains(data_id):
            # Executor wait just for signal
            # We use a blocking send here because the receiver is waiting for the result.
            communication.mpi_send_object(
                communication.MPIState.get_instance().comm,
                data_id,
                communication.MPIRank.ROOT,
            )
            logger.debug("Wait data {} id is ready".format(data_id._id))
        else:
            self.put(data_id, communication.MPIRank.ROOT, self.REQ_WAIT)
            logger.debug("Pending wait request {} id".format(data_id._id))

    def process_get_request(self, source_rank, data_id, is_blocking_op=False):
        """
        Satisfy GET operation request from another process.

        Save request for later processing if `data_id` is not available currently.

        Parameters
        ----------
        source_rank : int
            Rank number to send data to.
        data_id: unidist.core.backends.common.data_id.DataID
            `data_id` associated data to request
        is_blocking_op : bool, default: False
            Whether the get request should be blocking or not.
            If ``True``, the request should be processed immediatly
            even for a worker since it can get into controller mode.

        Notes
        -----
        Request is asynchronous, no wait for the data sending.
        """
        object_store = ObjectStore.get_instance()
        async_operations = AsyncOperations.get_instance()
        if object_store.contains(data_id):
            if source_rank == communication.MPIRank.ROOT or is_blocking_op:
                # The controller or a requesting worker is blocked by the request
                # which should be processed immediatly
                operation_data = object_store.get(data_id)
                # We use a blocking send here because the receiver is waiting for the result.
                communication.send_complex_data(
                    mpi_state.comm,
                    operation_data,
                    source_rank,
                )
            else:
                operation_type = common.Operation.PUT_DATA
                if object_store.is_already_serialized(data_id):
                    operation_data = object_store.get_serialized_data(data_id)
                    # Async send to avoid possible dead-lock between workers
                    h_list, _ = communication.isend_complex_operation(
                        mpi_state.comm,
                        operation_type,
                        operation_data,
                        source_rank,
                        is_serialized=True,
                    )
                    async_operations.extend(h_list)
                else:
                    operation_data = {
                        "id": data_id,
                        "data": object_store.get(data_id),
                    }
                    # Async send to avoid possible dead-lock between workers
                    h_list, serialized_data = communication.isend_complex_operation(
                        mpi_state.comm,
                        operation_type,
                        operation_data,
                        source_rank,
                        is_serialized=False,
                    )
                    async_operations.extend(h_list)
                    object_store.cache_serialized_data(data_id, serialized_data)

            logger.debug(
                "Send requested {} id to {} rank - PROCESSED".format(
                    data_id._id, source_rank
                )
            )
        else:
            logger.debug(
                "Pending request {} id to {} rank".format(data_id._id, source_rank)
            )
            self.put(data_id, source_rank, self.REQ_DATA, is_blocking_op=is_blocking_op)

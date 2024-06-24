# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""LazyDataset base classes.

There are 3 main classes:
- `LazyMapDataset` define a dataset that supports efficient random access. It
  has 3 important properties:
  - `__len__()` returns the length of a single epoch over the dataset.
  - `__getitem__()` will return the element at any given (positive) index. The
    "true" length of a `LazyMapDataset` is infinite. Many implementations will
    simply loop but exceptions exists (e.g. `ShuffleLazyMapDataset` will loop
    with a different order).
  - The dataset is lazy and individual elements are only created when calling
    `__getitem__()`. Most `LazyMapDatasets`s are statements and will not hold
    elements.
- `LazyIterDataset` defines a dataset that does not support efficient random
  access. It can still be iterated over. A `LazyMapDataset` can be turned into
  a `LazyIterDataset` but going from `LazyIterDataset` to `LazyMapDataset` might
  be as expensive as materializing the whole dataset.
  A `LazyIterDataset` can have known, unknown or infinite length.
- `LazyDatasetIterator` defines a stateful iterator over `LazyIterDataset`. The
  state of the iterator can be saved and restored.

Using the interfaces defined in `collections.abc` you can think of
LazyMapDataset as (infinite) Sequence, LazyIterDataset as Iterable and
LazyDatasetIterator as Iterator.
"""

from __future__ import annotations

import abc
import builtins
import collections
from collections.abc import Callable, Iterable, Iterator, Sequence
import contextlib
import copy
import functools
import queue
import threading
import time
from typing import Any, Mapping, Optional, Protocol, TypeVar, Union, overload

from concurrent import futures
from grain._src.core import monitoring as grain_monitoring
from grain._src.core import sharding
from grain._src.core import transforms
from grain._src.core import tree
from grain._src.core import usage_logging
import multiprocessing as mp
from grain._src.python import grain_pool
from grain._src.python import options as grain_options
from grain._src.python import shared_memory_array
import numpy as np

from grain._src.core import monitoring


_api_usage_counter = monitoring.Counter(
    "/grain/python/lazy_dataset/api",
    metadata=monitoring.Metadata(
        description="Lazy Dataset API initialization counter."
    ),
    root=grain_monitoring.get_monitoring_root(),
    fields=[("name", str)],
)

T = TypeVar("T")
_MAX_PREFETCH_THREADS = 1000


class RegisterableLazyMapDatasetFn(Protocol):
  """Interface for functions registered on all LazyMapDatasets."""

  def __call__(self, dataset: LazyMapDataset, *args, **kwargs) -> Any:
    ...


class RegisterableLazyIterDatasetFn(Protocol):
  """Interface for functions registered on all LazyIterDatasets."""

  def __call__(self, dataset: LazyIterDataset, *args, **kwargs) -> Any:
    ...


class LazyMapDataset(Sequence[T], abc.ABC):
  """Abstract base class for all LazyMapDataset classes."""

  _functions: dict[str, RegisterableLazyMapDatasetFn] = {}
  """Functions registered on all LazyMapdatasets via a decoration."""

  def __init__(
      self, parents: Union[LazyMapDataset, Sequence[LazyMapDataset]] = ()
  ):
    if isinstance(parents, LazyMapDataset):
      self._parents = (parents,)
    else:
      self._parents = tuple(parents)
    usage_logging.log_event("LazyMapDataset", tag_3="PyGrain")
    _api_usage_counter.Increment("LazyMapDataset")

  @property
  def parents(self) -> Sequence[LazyMapDataset]:
    return self._parents

  @property
  def _parent(self) -> LazyMapDataset:
    assert len(self._parents) == 1
    return self._parents[0]

  @abc.abstractmethod
  def __len__(self) -> int:
    """Returns the length of this dataset."""

  @overload
  def __getitem__(self, index: builtins.slice) -> LazyMapDataset:
    ...

  @overload
  def __getitem__(self, index: int) -> Optional[T]:
    ...

  @abc.abstractmethod
  def __getitem__(self, index):
    """Returns the element for the index or None if missing."""

  def filter(
      self, transform: transforms.FilterTransform | Callable[[T], bool]
  ) -> "LazyMapDataset[T]":
    """Returns a dataset containing only the elements that match the filter.

    Accessing an element of the returned dataset using subscription (`ds[i]`)
    returns:

    - `None` if `transform` returned `False`
    - the element if `transform` returned `True`

    Iterating over a filtered dataset skips `None` elements by default.

    The following expressions are equivalent:

    - `ds = ds.filter(lambda x: x > 5)`
    - `ds = FilterLazyMapDataset(ds, lambda x: x > 5)`

    The `ds.filter(...)` version allows chaining multiple transformations, e.g.,
    `ds = ds.filter(...).map(...).filter(...)`

    Args:
      transform: Either a `FilterTransform` containing the `filter` method or a
        callable that takes an element and returns a boolean.

    Returns:
      A dataset of the same type containing only the elements for which the
      filter transform returns `True`.
    """
    # Loaded lazily due to a circular dependency (lazy_dataset <-> filter).
    # pylint: disable=g-import-not-at-top
    from grain._src.python.lazy_dataset.transformations import filter as filter_dataset
    # pylint: enable=g-import-not-at-top
    return filter_dataset.FilterLazyMapDataset(parent=self, transform=transform)

  def shuffle(self, *, seed: int) -> "LazyMapDataset[T]":
    """Returns a dataset containing the same elements but in a shuffled order.

    The following expressions are equivalent:

    - `ds = ds.shuffle(seed=42)`
    - `ds = ShuffleLazyMapDataset(ds, seed=42)`

    The `ds.shuffle(...)` version allows chaining multiple transformations,
    e.g.,
    `ds = ds.filter(...).map(...).shuffle(...)`.

    Args:
      seed: An integer between 0 and 2**32-1 representing the seed used by the
        shuffling algorithm.

    Returns:
      A dataset containing the same elements but in a shuffled order.
    """
    # Loaded lazily due to a circular dependency (lazy_dataset <-> shuffle).
    # pylint: disable=g-import-not-at-top
    from grain._src.python.lazy_dataset.transformations import shuffle
    # pylint: enable=g-import-not-at-top
    return shuffle.ShuffleLazyMapDataset(parent=self, seed=seed)

  def slice(self, sl: builtins.slice) -> "LazyMapDataset[T]":
    """Returns a dataset containing only the elements with indices in `sl`.

    The following expressions are equivalent:

    - `ds = ds.slice(slice(1, 10, 2))`
    - `ds = SliceLazyMapDataset(ds, slice(1, 10, 2))`
    - `ds = ds[1:10:2]` (for `LazyMapDataset`s supporting `slice` objects in
      subscriptions)

    The `ds.slice(...)` and `ds[...]` versions allow chaining multiple
    transformations, e.g.,
    `ds = ds[10::4].filter(...).map(...)`.

    Args:
      sl: A `slice` object
        (https://docs.python.org/3/library/functions.html#slice) representing
        the slice of elements to that should constitute the returned dataset.

    Returns:
      A dataset containing only the elements with indices in the `sl` slice.
    """
    # Loaded lazily due to a circular dependency (lazy_dataset <-> slice).
    # pylint: disable=g-import-not-at-top
    from grain._src.python.lazy_dataset.transformations import slice as slice_dataset
    # pylint: enable=g-import-not-at-top
    return slice_dataset.SliceLazyMapDataset(parent=self, sl=sl)

  def repeat(self, num_epochs: int | None = None) -> "LazyMapDataset[T]":
    """Returns a dataset repeating the elements of this dataset multiple times.

    Specifying `None` for `num_epochs` will repeat the dataset infinitely, and
    causes `len(ds)` to return `sys.maxsize`.

    Since `LazyMapDataset`s allow accessing elements past `len(ds) - 1` anyway
    (and use the index modulo `len(ds)`), this transformation effectively only
    changes the length of the dataset.

    `repeat(...)` shouldn't be called on an infinite dataset.

    The following expressions are equivalent:

    - `ds = ds.repeat(42)`
    - `ds = RepeatLazyMapDataset(ds, 42)`

    The `ds.repeat(...)` version allows chaining multiple transformations, e.g.,
    `ds = ds.filter(...).map(...).repeat(...)`.

    Args:
      num_epochs: Either a positive integer representing the number of times
        this dataset should be repeated or `None` to repeat infinitely.

    Returns:
      A dataset repeating the elements of this dataset multiple times.
    """
    # Loaded lazily due to a circular dependency (lazy_dataset <-> repeat).
    # pylint: disable=g-import-not-at-top
    from grain._src.python.lazy_dataset.transformations import repeat
    # pylint: enable=g-import-not-at-top
    return repeat.RepeatLazyMapDataset(parent=self, num_epochs=num_epochs)

  @classmethod
  def register_function(cls, name: str, function: RegisterableLazyMapDatasetFn):
    if name in cls._functions:
      raise ValueError(
          f"Cannot register {function} as dataset function '{name}' since it's"
          f" already taken by {cls._functions[name]}."
      )
    cls._functions[name] = function

  def __getattr__(self, attribute_name: str):
    if attribute_name in LazyMapDataset._functions:
      return functools.partial(LazyMapDataset._functions[attribute_name], self)
    raise AttributeError(
        f"'{self.__class__.__name__}' object has no attribute"
        f" '{attribute_name}' :("
    )

  def __iter__(self) -> LazyDatasetIterator[T]:
    return self.to_iter_dataset().__iter__()

  def to_iter_dataset(
      self,
      read_options: Optional[grain_options.ReadOptions] = None,
      allow_nones: bool = False,
  ) -> LazyIterDataset[T]:
    """Syntactic sugar to construct a LazyIterDataset."""
    return PrefetchLazyIterDataset(
        self,
        read_options=read_options or grain_options.ReadOptions(),
        allow_nones=allow_nones,
    )


class LazyIterDataset(Iterable[T], abc.ABC):
  """Abstract base class for all LazyIterDataset classes."""

  _functions: dict[str, RegisterableLazyIterDatasetFn] = {}

  def __init__(
      self,
      parents: Union[
          LazyMapDataset,
          LazyIterDataset,
          Sequence[Union[LazyMapDataset, LazyIterDataset]],
      ] = (),
  ):
    if isinstance(parents, (LazyMapDataset, LazyIterDataset)):
      self._parents = (parents,)
    else:
      self._parents = tuple(parents)
    usage_logging.log_event("LazyIterDataset", tag_3="PyGrain")
    _api_usage_counter.Increment("LazyIterDataset")

  @property
  def parents(self) -> Sequence[Union[LazyMapDataset, LazyIterDataset]]:
    return self._parents

  @property
  def _parent(self) -> Union[LazyMapDataset, LazyIterDataset]:
    assert len(self._parents) == 1, self._parents
    return self._parents[0]

  def filter(
      self, transform: transforms.FilterTransform | Callable[[T], bool]
  ) -> "LazyIterDataset[T]":
    """Returns a dataset containing only the elements that match the filter.

    `ds = ds.filter(lambda x: x > 5)`
    is equivalent to
    `ds = FilterLazyIterDataset(ds, lambda x: x > 5)`

    Args:
      transform: Either a `FilterTransform` containing the `filter` method or a
        callable that takes an element and returns a boolean.

    Returns:
      A dataset of the same type containing only the elements for which the
      filter transform returns `True`.
    """
    # Loaded lazily due to a circular dependency (lazy_dataset <-> filter).
    # pylint: disable=g-import-not-at-top
    from grain._src.python.lazy_dataset.transformations import filter as filter_dataset
    # pylint: enable=g-import-not-at-top
    return filter_dataset.FilterLazyIterDataset(
        parent=self, transform=transform
    )

  def set_parent_maps_slice(self, sl: slice) -> None:
    """Replaces LazyMapDataset-type parents with their sliced versions.

    Applies recursively for LazyIterDataset-type parents.

    Args:
     sl: slice to apply.
    """
    sliced_parents = []
    for parent in self._parents:
      if isinstance(parent, LazyMapDataset):
        sliced_parents.append(parent.slice(sl))
      else:
        parent.set_parent_maps_slice(sl)
        sliced_parents.append(parent)
    self._parents = tuple(sliced_parents)

  @abc.abstractmethod
  def __iter__(self) -> LazyDatasetIterator[T]:
    """Returns an iterator for this dataset."""

  @classmethod
  def register_function(cls, name: str, function: RegisterableLazyMapDatasetFn):
    if name in cls._functions:
      raise ValueError(
          f"Cannot register {function} as dataset function '{name}' since it's"
          f" already taken by {cls._functions[name]}."
      )
    cls._functions[name] = function

  def __getattr__(self, attribute_name: str):
    if attribute_name in LazyIterDataset._functions:
      return functools.partial(LazyIterDataset._functions[attribute_name], self)
    raise AttributeError(
        f"'{self.__class__.__name__}' object has no attribute"
        f" '{attribute_name}' :("
    )


def lazy_map_dataset_function(name: str):
  """Registers a function as a LazyMapDataset function."""

  def _fn(cls):
    LazyMapDataset.register_function(name=name, function=cls)
    return cls

  return _fn


def lazy_iter_dataset_function(name: str):
  """Registers a function as a LazyIterDataset function."""

  def _fn(cls):
    LazyIterDataset.register_function(name=name, function=cls)
    return cls

  return _fn


class LazyDatasetIterator(Iterator[T], abc.ABC):
  """Abstract base class for all LazyIterDataset iterator classes."""

  def __iter__(self) -> LazyDatasetIterator[T]:
    return self

  # __next__ abstract method since we inherit from Iterator[T].

  @abc.abstractmethod
  def get_state(self) -> dict[str, Any]:
    """Returns the current state of the iterator."""

  @abc.abstractmethod
  def set_state(self, state: dict[str, Any]):
    """Sets the current state of the iterator."""


@lazy_map_dataset_function("prefetch")
class PrefetchLazyIterDataset(LazyIterDataset[T]):
  """Iterable dataset that uses a thread pool for prefetching."""

  def __init__(
      self,
      parent: LazyMapDataset[T],
      *,
      read_options: grain_options.ReadOptions,
      allow_nones: bool = False,
  ):
    super().__init__(parent)
    self._read_options = read_options
    self._allow_nones = allow_nones

  def __iter__(self) -> LazyDatasetIterator[T]:
    return PrefetchLazyDatasetIterator(
        self._parent, self._read_options, self._allow_nones
    )


class PrefetchLazyDatasetIterator(LazyDatasetIterator[T]):
  """Iterator that performs prefetching using a thread pool."""

  def __init__(
      self,
      dataset: LazyMapDataset[T],
      read_options: grain_options.ReadOptions,
      allow_nones: bool,
  ):
    super().__init__()
    self._dataset = dataset
    self._dataset_length = len(dataset)
    self._next_index = 0
    self._buffer = None
    self._prefetch_buffer_size = read_options.prefetch_buffer_size
    self._allow_nones = allow_nones
    if self._prefetch_buffer_size > 0:
      self._executor = futures.ThreadPoolExecutor(read_options.num_threads)

  def __next__(self) -> T:
    # We loop here to skip all None elements (in case the underlying dataset
    # is sparse), if self._allow_nones = False, else we return Nones too.
    while True:
      if self._next_index == self._dataset_length:
        break
      if self._prefetch_buffer_size > 0:
        if not self._buffer:
          indices = range(
              self._next_index,
              min(
                  self._next_index + self._prefetch_buffer_size,
                  self._dataset_length,
              ),
          )
          self._buffer = collections.deque(
              self._executor.submit(self._dataset.__getitem__, i)
              for i in indices
          )
        element = self._buffer.popleft()
        if self._next_index + self._prefetch_buffer_size < self._dataset_length:
          self._buffer.append(
              self._executor.submit(
                  self._dataset.__getitem__,
                  self._next_index + self._prefetch_buffer_size,
              )
          )
        element = element.result()
      else:
        element = self._dataset[self._next_index]
      self._next_index += 1
      if self._allow_nones or element is not None:
        return element
    raise StopIteration

  def get_state(self):
    return {"next_index": self._next_index}

  def set_state(self, state):
    self._next_index = state["next_index"]
    if self._prefetch_buffer_size > 0:
      self._buffer = None


def _iterator_with_context(
    iterator: contextlib.AbstractContextManager[Iterator[T]],
) -> Iterator[T]:
  with iterator as it:
    yield from it


@lazy_iter_dataset_function("prefetch")
class MultiprocessPrefetchLazyIterDataset(LazyIterDataset[T]):
  """Uses a pool of processes to prefetch elements ahead of time.

  It usually makes sense to add this transformation in the end of the pipeline
  since it will execute the parent LazyIterDataset in multiple processes.
  """

  def __init__(
      self,
      parent: LazyIterDataset[T],
      multiprocessing_options: grain_options.MultiprocessingOptions,
  ):
    if multiprocessing_options.num_workers < 1:
      raise ValueError(
          "`num_workers` must be greater than 0, got "
          f"{multiprocessing_options.num_workers}."
      )
    super().__init__(parent)
    self._validate_parent_dataset()
    self._multiprocessing_options = multiprocessing_options

  def _validate_parent_dataset(self):
    """Checks that there's a single level of parallelization."""
    to_check = [self._parent]
    while to_check:
      dataset = to_check.pop(0)
      if isinstance(dataset, MultiprocessPrefetchLazyIterDataset):
        raise ValueError(
            "Having multiple `MultiprocessPrefetchLazyIterDataset`s is not "
            "allowed. Consider only keeping the last one."
        )
      to_check.extend(dataset.parents)

  def __iter__(self) -> MultiprocessPrefetchLazyDatasetIterator[T]:
    return MultiprocessPrefetchLazyDatasetIterator(
        self._parent, self._multiprocessing_options
    )


# Keys in `MultiprocessPrefetchLazyDatasetIterator` checkpoints.
_WORKERS_STATE = "workers_state"
_ITERATIONS_TO_SKIP = "iterations_to_skip"
_LAST_WORKER_INDEX = "last_worker_index"

# Minimal interval (in seconds) between consecutive state recordings in worker
# processes of `MultiprocessPrefetchLazyDatasetIterator`. We record the state
# periodically to reduce the overhead of sending the state from workers.
# Note that this is also an approximate upper bound on how long it is going to
# take to recover from a checkpointed state. Larger values will decrease the
# overhead of sending the updated state but will also make recovery from a
# checkpoint longer on average.
_RECORD_STATE_INTERVAL_S = 3


def _copy_leaf_to_shm(leaf: Any) -> Any:
  """Copies `leaf` to shared memory if it's a numpy array."""
  if (
      not isinstance(leaf, np.ndarray)
      or leaf.dtype.hasobject
      or not leaf.flags.c_contiguous
  ):
    return leaf

  shared_memory_arr = shared_memory_array.SharedMemoryArray(
      leaf.shape, leaf.dtype
  )
  np.copyto(shared_memory_arr, leaf, casting="no")
  return shared_memory_arr.metadata


def _copy_struct_to_shm(struct: Any) -> Any:
  """Copies leaf ndarrays of the structure to shared memory."""
  return tree.map_structure(_copy_leaf_to_shm, struct)


def _open_leaf_from_shm(leaf: Any) -> Any:
  """Recovers `leaf` from shared memory if it's a numpy array metadata."""
  if isinstance(leaf, shared_memory_array.SharedMemoryArrayMetadata):
    leaf = shared_memory_array.SharedMemoryArray.from_metadata(leaf)
    leaf.unlink_on_del()
  return leaf


def _open_struct_from_shm(struct: Any) -> Any:
  """Recovers leaf ndarrays of the structure from shared memory."""
  return tree.map_structure(_open_leaf_from_shm, struct)


class MultiprocessPrefetchLazyDatasetIterator(LazyDatasetIterator[T]):
  """Iterator that performs prefetching using a multiprocessing pool."""

  def __init__(
      self,
      parent: LazyIterDataset[T],
      multiprocessing_options: grain_options.MultiprocessingOptions,
  ):
    super().__init__()
    self._parent = parent
    self._multiprocessing_options = multiprocessing_options
    # The underlying iterator producing elements and workers state.
    self._iterator = None
    # Raw reference to the underlying iterator that can be used to determine the
    # last worker index.
    self._raw_iterator = None
    # Create initial state. We record state of each worker periodically together
    # with the number of iterations without the recorded state and index of the
    # last worker.
    workers_state = {}
    iterations_to_skip = {}
    for i in range(multiprocessing_options.num_workers):
      workers_state[str(i)] = iter(self._parent).get_state()  # pytype: disable=attribute-error
      iterations_to_skip[str(i)] = 0

    self._state = {
        _WORKERS_STATE: workers_state,
        _ITERATIONS_TO_SKIP: iterations_to_skip,
        _LAST_WORKER_INDEX: -1,
    }

  def __iter__(self) -> LazyDatasetIterator[T]:
    return self

  def __next__(self) -> T:
    self._ensure_iterator_initialized()
    result, state = next(self._iterator)
    worker_index = self._raw_iterator.get_last_worker_index()  # pytype: disable=attribute-error
    self._state[_LAST_WORKER_INDEX] = worker_index
    worker_index_str = str(worker_index)
    if state is None:
      self._state[_ITERATIONS_TO_SKIP][worker_index_str] += 1
    else:
      self._state[_ITERATIONS_TO_SKIP][worker_index_str] = 0
      self._state[_WORKERS_STATE][worker_index_str] = state
    return _open_struct_from_shm(result)

  def start_prefetch(self) -> None:
    """Prefetches elements from the iterator.

    This will run background processes for prefetching. To make sure to clean up
    the resources, it should be followed by at least one `next` call.
    """
    self._ensure_iterator_initialized()

  def set_state(self, state) -> None:
    self._state = state
    self._raw_iterator = None
    self._iterator = None

  def get_state(self) -> dict[str, Any]:
    return copy.deepcopy(self._state)

  def _ensure_iterator_initialized(self) -> None:
    if self._iterator is None:
      self._raw_iterator = self._create_iterator_context()
      self._raw_iterator.start_prefetch()
      self._iterator = _iterator_with_context(self._raw_iterator)

  def _create_iterator_context(self) -> grain_pool.MultiProcessIterator[T]:
    """Creates a `MultiProcessIterator`."""

    state = self._state
    parent = self._parent

    def get_element_producer_fn(
        worker_index: int, worker_count: int
    ) -> Iterator[tuple[T, Optional[dict[str, Any]]]]:
      # Recover from the last recorded state for the given worker.
      worker_state = state[_WORKERS_STATE][str(worker_index)]
      parent.set_parent_maps_slice(slice(worker_index, None, worker_count))
      it = iter(parent)
      it.set_state(worker_state)  # pytype: disable=attribute-error
      # Skip the required number of iterations after the last recorded state.
      for _ in range(state[_ITERATIONS_TO_SKIP][str(worker_index)]):
        _ = next(it)
      last_recorded_state_time = time.time()
      for element in it:
        now = time.time()
        element = _copy_struct_to_shm(element)
        if now - last_recorded_state_time >= _RECORD_STATE_INTERVAL_S:
          last_recorded_state_time = now
          yield (element, it.get_state())  # pytype: disable=attribute-error
        else:
          yield (element, None)

    return grain_pool.MultiProcessIterator(
        get_element_producer_fn,
        self._multiprocessing_options,
        (self._state[_LAST_WORKER_INDEX] + 1)
        % self._multiprocessing_options.num_workers,
    )


class ThreadPrefetchLazyIterDataset(LazyIterDataset[T]):
  """Iterable dataset that uses a synchronized queue for prefetching.

  This is a thread-based alternative to `MultiprocessPrefetchLazyIterDataset`.

  Attributes:
    parent: The parent dataset to prefetch from.
    prefetch_buffer_size: The size of the prefetch buffer.
  """

  def __init__(
      self,
      parent: LazyIterDataset[T],
      *,
      prefetch_buffer_size: int,
  ):
    super().__init__(parent)
    self._prefetch_buffer_size = prefetch_buffer_size

  def __iter__(self) -> ThreadPrefetchLazyDatasetIterator[T]:
    return ThreadPrefetchLazyDatasetIterator(
        self._parent, self._prefetch_buffer_size
    )


# Type for the iterator state.
StateT = Mapping[str, Any]


# Representation of the initial state, pre-next.
_INITIAL_STATE_SENTINEL = object()


class ThreadPrefetchLazyDatasetIterator(LazyDatasetIterator[T]):
  """Iterator that performs prefetching using a synchronized queue."""

  def __init__(
      self,
      dataset: LazyIterDataset[T],
      prefetch_buffer_size: int,
  ):
    super().__init__()
    self._dataset: LazyIterDataset[T] = dataset
    self._iterator: LazyDatasetIterator[T] = dataset.__iter__()
    self._prefetch_buffer_size = prefetch_buffer_size
    self._state: StateT | None = None

    self._work_queue = queue.Queue[Callable[[], Any]]()
    self._work_thread: threading.Thread | None = None
    # Whether this iterator is closed, meaning it should no longer be used.
    self._closed = False
    self._producer_running: threading.Event = None
    self._buffer: queue.Queue[tuple[T, StateT, Exception | None]] = None

  def _start_producer(self, initial_state: None):
    """Starts the producer.

    Args:
      initial_state: An optional initial state to set on the delegate.

    Raises:
      ValueError: If the iterator has been closed, or if the producer is already
        running.
    """
    if self._closed:
      raise ValueError("Attempting to use a closed iterator.")
    if self._producer_running is not None:
      raise ValueError("The producer is already running.")

    if self._work_thread is None:
      self._work_thread = threading.Thread(
          target=self._work_loop, daemon=True, name=f"Prefetch-{self._dataset}"
      )
      self._work_thread.start()

    self._state = initial_state
    self._producer_running = threading.Event()
    self._producer_running.set()
    self._buffer = queue.Queue(maxsize=self._prefetch_buffer_size)
    self._work_queue.put(
        functools.partial(
            self._producer,
            initial_state=initial_state,
            output_buffer=self._buffer,
            running=self._producer_running,
        )
    )

  def _producer(
      self,
      initial_state,
      output_buffer: queue.Queue[tuple[T, StateT, Exception | None]],
      running: threading.Event,
  ) -> None:
    """Functor that fills the queue to its capacity.

    Should be run on a separate thread.

    Args:
      initial_state: state to initialize the itertor to.
      output_buffer: queue to fill.
      running: an sync event for whether the thread should run.
    """
    try:
      if initial_state is not None:
        self._iterator.set_state(initial_state)
      else:
        # Put the initial state of the iterator with a sentinel value, which
        # will be discarded. This avoids having to call a potentially expensive
        # and unused get_state() on the main thread.
        output_buffer.put(
            (_INITIAL_STATE_SENTINEL, self._iterator.get_state(), None)
        )
      # Check if the producer thread should be running every time an item is
      # retrieved from the queue.
      while running.is_set():
        while True:
          element, state = next(self._iterator), self._iterator.get_state()
          output_buffer.put((element, state, None))
          break
    except Exception as e:  # pylint: disable=broad-except
      output_buffer.put((None, None, e))

  def __next__(self):
    self.start_prefetch()
    assert self._buffer is not None
    element, state, err = self._buffer.get()

    if err is not None:
      raise err
    if self._state is None or element is _INITIAL_STATE_SENTINEL:
      # Both conditions should be simultaneously true and only once.
      assert element is _INITIAL_STATE_SENTINEL
      if self._state is not None:
        raise AssertionError(f"Expected {self._state=} to be None. {state=}.")
      self._state = state
      # Current call has retrieved a sentinel value and the initial state,
      # make another call to retrieve the actual first value from the delegate
      # iterator.
      return next(self)
    else:
      self._state = state
      return element

  def close(self):
    """Stops the iterator. No further calls to the iterator are expected."""
    self._closed = True
    self._stop_producer()
    # Make sure the work thread isn't blocked, so it can exit.
    self._work_queue.put(lambda: None)

  def start_prefetch(self):
    """Starts the producer if it's not already running.

    Raises:
      ValueError: If the iterator has been closed, or if there's already a
        running producer.
    """
    if self._closed:
      raise ValueError("Attempting to use a closed iterator.")
    if self._producer_running is None:
      self._start_producer(None)

  def _stop_producer(self):
    """Stops the producer if it's currently running."""
    producer_running = self._producer_running
    buffer = self._buffer
    if producer_running is None:
      # Nothing to stop.
      return

    producer_running.clear()
    # Remove entries from the buffer to unblock the producer, so that it checks
    # producer_running.is_set() and exits.
    assert buffer is not None  # PyType.
    while True:
      try:
        buffer.get_nowait()
      except queue.Empty:
        break
    self._producer_running = None
    self._buffer = None

  def get_state(self):
    self.start_prefetch()
    if self._state is None:
      # `__next__` has not been called, the first tuple in the buffer should be
      # made up of the `_INITIAL_STATE_SENTINEL` value and the initial state of
      # the delegate iterator.
      buffer = self._buffer
      assert buffer is not None  # PyType.
      val, state, err = buffer.get()
      if err is not None:
        raise err
      assert val is _INITIAL_STATE_SENTINEL
      assert state is not None
      self._state = state
    return self._state

  def set_state(self, state):
    self._stop_producer()
    self._state = state
    if self._prefetch_buffer_size > 0:
      self._buffer = None
    self._start_producer(state)

  def _work_loop(self):
    while not self._closed:
      self._work_queue.get()()

  def _start_worker(self):
    if self._work_thread is None:
      self._work_thread = threading.Thread(
          target=self._work_loop, daemon=True, name=f"Prefetch-{self._dataset}"
      )
      self._work_thread.start()


class RangeLazyMapDataset(LazyMapDataset[int]):
  """Range data source, similar to python range() function."""

  def __init__(self, start: int, stop: Optional[int] = None, step: int = 1):
    super().__init__()
    self.start = 0 if stop is None else start
    self.stop = start if stop is None else stop
    self.step = step

  @functools.cached_property
  def _length(self) -> int:
    return len(range(self.start, self.stop, self.step))

  def __len__(self) -> int:
    return self._length

  def __getitem__(self, index):
    if isinstance(index, slice):
      return self.slice(index)
    return self.start + (index % self._length) * self.step

  def to_iter_dataset(
      self,
      read_options: Optional[grain_options.ReadOptions] = None,
      allow_nones: bool = False,
  ) -> LazyIterDataset[int]:
    """Syntactic sugar to construct a LazyIterDataset."""
    return PrefetchLazyIterDataset(
        self,
        read_options=(
            read_options or grain_options.ReadOptions(prefetch_buffer_size=0)
        ),
        allow_nones=allow_nones,
    )


# Deprecated: This class should not be used for new code. It's used to
# implement the stateless Sampler.
# For new code the PrefetchLazyMapDataset should be used to implement sharding.
class ShardLazyDataset(LazyMapDataset[T]):
  """Shards the parent into consecutive pieces."""

  def __init__(
      self, parent: LazyMapDataset[T], shard_options: sharding.ShardOptions
  ):
    super().__init__(parent)
    self._start, self._end = sharding.even_split(
        len(self._parent), shard_options
    )

  def __len__(self) -> int:
    return self._end - self._start

  def __getitem__(self, index: Union[int, slice]) -> Optional[T]:
    if isinstance(index, slice):
      return self.slice(index)
    epoch = index // len(self)
    index_in_epoch = index % len(self)
    index = epoch * len(self._parent) + index_in_epoch + self._start
    return self._parent[index]

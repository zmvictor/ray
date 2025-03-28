import itertools
import sys
from typing import List, Optional
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

import ray
from ray.data._internal.datasource.parquet_datasink import ParquetDatasink
from ray.data._internal.execution.interfaces.op_runtime_metrics import OpRuntimeMetrics
from ray.data._internal.execution.operators.base_physical_operator import (
    AllToAllOperator,
)
from ray.data._internal.execution.operators.input_data_buffer import InputDataBuffer
from ray.data._internal.execution.operators.map_operator import MapOperator
from ray.data._internal.execution.operators.map_transformer import (
    BatchMapTransformFn,
    BlockMapTransformFn,
    BlocksToBatchesMapTransformFn,
    BuildOutputBlocksMapTransformFn,
)
from ray.data._internal.execution.operators.task_pool_map_operator import (
    TaskPoolMapOperator,
)
from ray.data._internal.execution.operators.zip_operator import ZipOperator
from ray.data._internal.logical.interfaces import LogicalPlan
from ray.data._internal.logical.interfaces.physical_plan import PhysicalPlan
from ray.data._internal.logical.operators.all_to_all_operator import (
    Aggregate,
    RandomShuffle,
    Repartition,
    Sort,
)
from ray.data._internal.logical.operators.from_operators import (
    FromArrow,
    FromItems,
    FromNumpy,
    FromPandas,
)
from ray.data._internal.logical.operators.map_operator import (
    Filter,
    FlatMap,
    MapBatches,
    MapRows,
    Project,
)
from ray.data._internal.logical.operators.n_ary_operator import Zip
from ray.data._internal.logical.operators.write_operator import Write
from ray.data._internal.logical.optimizers import PhysicalOptimizer
from ray.data._internal.logical.rules.configure_map_task_memory import (
    ConfigureMapTaskMemoryUsingOutputSize,
)
from ray.data._internal.logical.util import (
    _op_name_white_list,
    _recorded_operators,
    _recorded_operators_lock,
)
from ray.data._internal.planner.exchange.sort_task_spec import SortKey
from ray.data._internal.planner.planner import Planner
from ray.data._internal.stats import DatasetStats
from ray.data.aggregate import Count
from ray.data.block import BlockMetadata
from ray.data.context import DataContext
from ray.data.datasource import Datasource
from ray.data.datasource.datasource import ReadTask
from ray.data.tests.conftest import *  # noqa
from ray.data.tests.test_util import get_parquet_read_logical_op
from ray.data.tests.util import column_udf, extract_values, named_values
from ray.tests.conftest import *  # noqa


def _check_usage_record(op_names: List[str], clear_after_check: Optional[bool] = True):
    """Check if operators with given names in `op_names` have been used.
    If `clear_after_check` is True, we clear the list of recorded operators
    (so that subsequent checks do not use existing records of operator usage)."""
    for op_name in op_names:
        assert op_name in _op_name_white_list
        with _recorded_operators_lock:
            assert _recorded_operators.get(op_name, 0) > 0, (
                op_name,
                _recorded_operators,
            )
    if clear_after_check:
        with _recorded_operators_lock:
            _recorded_operators.clear()


def _check_valid_plan_and_result(
    ds,
    expected_plan,
    expected_result,
    expected_physical_plan_ops=None,
):
    assert ds.take_all() == expected_result
    assert str(ds._plan._logical_plan.dag) == expected_plan

    expected_physical_plan_ops = expected_physical_plan_ops or []
    for op in expected_physical_plan_ops:
        assert op in ds.stats(), f"Operator {op} not found: {ds.stats()}"


def test_read_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()
    planner = Planner()
    op = get_parquet_read_logical_op()
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "ReadParquet"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)
    assert physical_op.actual_target_max_block_size == ctx.target_max_block_size
    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]
    assert physical_op.input_dependencies[0]._logical_operators == [op]


def test_read_operator_emits_warning_for_large_read_tasks():
    class StubDatasource(Datasource):
        def estimate_inmemory_data_size(self) -> Optional[int]:
            return None

        def get_read_tasks(self, parallelism: int) -> List[ReadTask]:
            large_object = np.zeros((128, 1024, 1024), dtype=np.uint8)  # 128 MiB

            def read_fn():
                _ = large_object
                yield pd.DataFrame({"column": [0]})

            return [ReadTask(read_fn, BlockMetadata(1, None, None, None, None))]

    with pytest.warns(UserWarning):
        ray.data.read_datasource(StubDatasource()).materialize()


def test_split_blocks_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    op = get_parquet_read_logical_op(parallelism=10)
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert physical_op.name == "ReadParquet->SplitBlocks(10)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )
    assert physical_op._additional_split_factor == 10

    # Test that split blocks prevents fusion.
    op = MapBatches(
        op,
        lambda x: x,
    )
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag
    assert physical_op.name == "MapBatches(<lambda>)"
    assert len(physical_op.input_dependencies) == 1
    up_physical_op = physical_op.input_dependencies[0]
    assert isinstance(up_physical_op, MapOperator)
    assert up_physical_op.name == "ReadParquet->SplitBlocks(10)"


def test_from_operators(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    op_classes = [
        FromArrow,
        FromItems,
        FromNumpy,
        FromPandas,
    ]
    for op_cls in op_classes:
        planner = Planner()
        op = op_cls([], [])
        plan = LogicalPlan(op, ctx)
        physical_op = planner.plan(plan).dag

        assert op.name == op_cls.__name__
        assert isinstance(physical_op, InputDataBuffer)
        assert len(physical_op.input_dependencies) == 0

        # Check that the linked logical operator is the same the input op.
        assert physical_op._logical_operators == [op]


def test_from_items_e2e(ray_start_regular_shared_2_cpus):
    data = ["Hello", "World"]
    ds = ray.data.from_items(data)
    assert ds.take_all() == named_values("item", data), ds

    # Check that metadata fetch is included in stats.
    assert "FromItems" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromItems"
    _check_usage_record(["FromItems"])


def test_map_operator_udf_name(ray_start_regular_shared_2_cpus):
    # Test the name of the Map operator with different types of UDF.
    def normal_function(x):
        return x

    lambda_function = lambda x: x  # noqa: E731

    class CallableClass:
        def __call__(self, x):
            return x

    class NormalClass:
        def method(self, x):
            return x

    udf_list = [
        # A nomral function.
        normal_function,
        # A lambda function
        lambda_function,
        # A callable class.
        CallableClass,
        # An instance of a callable class.
        CallableClass(),
        # A normal class method.
        NormalClass().method,
    ]

    expected_names = [
        "normal_function",
        "<lambda>",
        "CallableClass",
        "CallableClass",
        "NormalClass.method",
    ]

    for udf, expected_name in zip(udf_list, expected_names):
        op = MapRows(
            get_parquet_read_logical_op(),
            udf,
        )
        assert op.name == f"Map({expected_name})"


def test_map_batches_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = MapBatches(
        read_op,
        lambda x: x,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "MapBatches(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


def test_map_batches_e2e(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(5)
    ds = ds.map_batches(column_udf("id", lambda x: x))
    assert extract_values("id", ds.take_all()) == list(range(5)), ds
    _check_usage_record(["ReadRange", "MapBatches"])


def test_map_rows_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = MapRows(
        read_op,
        lambda x: x,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Map(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)


def test_map_rows_e2e(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(5)
    ds = ds.map(column_udf("id", lambda x: x + 1))
    expected = [1, 2, 3, 4, 5]
    actual = sorted(extract_values("id", ds.take_all()))
    assert actual == expected, f"Expected {expected}, but got {actual}"
    _check_usage_record(["ReadRange", "Map"])


def test_filter_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = Filter(
        read_op,
        lambda x: x,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Filter(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )


def test_filter_e2e(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(5)
    ds = ds.filter(fn=lambda x: x["id"] % 2 == 0)
    assert sorted(extract_values("id", ds.take_all())) == [0, 2, 4], ds
    _check_usage_record(["ReadRange", "Filter"])


def test_project_operator_select(ray_start_regular_shared_2_cpus):
    """
    Checks that the physical plan is properly generated for the Project operator from
    select columns.
    """
    path = "example://iris.parquet"
    ds = ray.data.read_parquet(path)
    ds = ds.map_batches(lambda d: d)
    cols = ["sepal.length", "petal.width"]
    ds = ds.select_columns(cols)

    logical_plan = ds._plan._logical_plan
    op = logical_plan.dag
    assert isinstance(op, Project), op.name
    assert op.cols == cols

    physical_plan = Planner().plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag
    assert isinstance(physical_op, TaskPoolMapOperator)
    assert isinstance(physical_op.input_dependency, TaskPoolMapOperator)


def test_project_operator_rename(ray_start_regular_shared_2_cpus):
    """
    Checks that the physical plan is properly generated for the Project operator from
    rename columns.
    """
    path = "example://iris.parquet"
    ds = ray.data.read_parquet(path)
    ds = ds.map_batches(lambda d: d)
    cols_rename = {"sepal.length": "sepal_length", "petal.width": "pedal_width"}
    ds = ds.rename_columns(cols_rename)

    logical_plan = ds._plan._logical_plan
    op = logical_plan.dag
    assert isinstance(op, Project), op.name
    assert not op.cols
    assert op.cols_rename == cols_rename

    physical_plan = Planner().plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag
    assert isinstance(physical_op, TaskPoolMapOperator)
    assert isinstance(physical_op.input_dependency, TaskPoolMapOperator)


def test_flat_map(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = FlatMap(
        read_op,
        lambda x: x,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "FlatMap(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )


def test_flat_map_e2e(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(2)
    ds = ds.flat_map(fn=lambda x: [{"id": x["id"]}, {"id": x["id"]}])
    assert extract_values("id", ds.take_all()) == [0, 0, 1, 1], ds
    _check_usage_record(["ReadRange", "FlatMap"])


def test_column_ops_e2e(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(2)
    ds = ds.add_column(fn=lambda df: df.iloc[:, 0], col="new_col")
    assert ds.take_all() == [{"id": 0, "new_col": 0}, {"id": 1, "new_col": 1}], ds
    _check_usage_record(["ReadRange", "MapBatches"])

    select_ds = ds.select_columns(cols=["new_col"])
    assert select_ds.take_all() == [{"new_col": 0}, {"new_col": 1}]
    _check_usage_record(["ReadRange", "MapBatches"])

    ds = ds.drop_columns(cols=["new_col"])
    assert ds.take_all() == [{"id": 0}, {"id": 1}], ds
    _check_usage_record(["ReadRange", "MapBatches"])


def test_random_sample_e2e(ray_start_regular_shared_2_cpus):
    import math

    def ensure_sample_size_close(dataset, sample_percent=0.5):
        r1 = ds.random_sample(sample_percent)
        assert math.isclose(
            r1.count(), int(ds.count() * sample_percent), rel_tol=2, abs_tol=2
        )

    ds = ray.data.range(10, override_num_blocks=2)
    ensure_sample_size_close(ds)

    ds = ray.data.range(10, override_num_blocks=2)
    ensure_sample_size_close(ds)

    ds = ray.data.range_tensor(5, override_num_blocks=2, shape=(2, 2))
    ensure_sample_size_close(ds)

    _check_usage_record(["ReadRange", "MapBatches"])


def test_random_shuffle_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = RandomShuffle(
        read_op,
        seed=0,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "RandomShuffle"
    assert isinstance(physical_op, AllToAllOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_shuffle_max_block_size
    )

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


def test_random_shuffle_e2e(ray_start_regular_shared_2_cpus, configure_shuffle_method):
    ds = ray.data.range(12, override_num_blocks=4)
    r1 = extract_values("id", ds.random_shuffle(seed=0).take_all())
    r2 = extract_values("id", ds.random_shuffle(seed=1024).take_all())
    assert r1 != r2, (r1, r2)
    assert sorted(r1) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], r1
    assert sorted(r2) == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11], r2
    _check_usage_record(["ReadRange", "RandomShuffle"])


@pytest.mark.parametrize(
    "shuffle",
    [True, False],
)
def test_repartition_operator(ray_start_regular_shared_2_cpus, shuffle):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = Repartition(read_op, num_outputs=5, shuffle=shuffle)
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Repartition"
    assert isinstance(physical_op, AllToAllOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    if shuffle:
        assert (
            physical_op.actual_target_max_block_size
            == DataContext.get_current().target_shuffle_max_block_size
        )
    else:
        assert (
            physical_op.actual_target_max_block_size
            == DataContext.get_current().target_max_block_size
        )

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


@pytest.mark.parametrize(
    "shuffle",
    [True, False],
)
def test_repartition_e2e(
    ray_start_regular_shared_2_cpus, configure_shuffle_method, shuffle
):
    def _check_repartition_usage_and_stats(ds):
        _check_usage_record(["ReadRange", "Repartition"])
        ds_stats: DatasetStats = ds._plan.stats()
        if shuffle:
            assert ds_stats.base_name == "ReadRange->Repartition"
            assert "ReadRange->RepartitionMap" in ds_stats.metadata
        else:
            assert ds_stats.base_name == "Repartition"
            assert "RepartitionSplit" in ds_stats.metadata
        assert "RepartitionReduce" in ds_stats.metadata

    ds = ray.data.range(10000, override_num_blocks=10).repartition(20, shuffle=shuffle)
    assert ds._plan.initial_num_blocks() == 20, ds._plan.initial_num_blocks()
    assert ds.sum() == sum(range(10000))
    assert ds._block_num_rows() == [500] * 20, ds._block_num_rows()
    _check_repartition_usage_and_stats(ds)

    # Test num_output_blocks > num_rows to trigger empty block handling.
    ds = ray.data.range(20, override_num_blocks=10).repartition(40, shuffle=shuffle)
    assert ds._plan.initial_num_blocks() == 40, ds._plan.initial_num_blocks()
    assert ds.sum() == sum(range(20))
    if shuffle:
        assert ds._block_num_rows() == [10] * 2 + [0] * (40 - 2), ds._block_num_rows()
    else:
        assert ds._block_num_rows() == [1] * 20 + [0] * 20, ds._block_num_rows()
    _check_repartition_usage_and_stats(ds)

    # Test case where number of rows does not divide equally into num_output_blocks.
    ds = ray.data.range(22).repartition(4, shuffle=shuffle)
    assert ds._plan.initial_num_blocks() == 4, ds._plan.initial_num_blocks()
    assert ds.sum() == sum(range(22))
    if shuffle:
        assert ds._block_num_rows() == [9, 9, 4, 0], ds._block_num_rows()
    else:
        assert ds._block_num_rows() == [5, 6, 5, 6], ds._block_num_rows()
    _check_repartition_usage_and_stats(ds)

    # Test case where we do not split on repartitioning.
    ds = ray.data.range(10, override_num_blocks=1).repartition(1, shuffle=shuffle)
    assert ds._plan.initial_num_blocks() == 1, ds._plan.initial_num_blocks()
    assert ds.sum() == sum(range(10))
    assert ds._block_num_rows() == [10], ds._block_num_rows()
    _check_repartition_usage_and_stats(ds)


def test_read_map_batches_operator_fusion(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    # Test that Read is fused with MapBatches.
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    op = MapBatches(
        read_op,
        lambda x: x,
    )
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert op.name == "MapBatches(<lambda>)"
    assert physical_op.name == "ReadParquet->MapBatches(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    input = physical_op.input_dependencies[0]
    assert isinstance(input, InputDataBuffer)
    assert physical_op in input.output_dependencies, input.output_dependencies
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )
    assert physical_op._logical_operators == [read_op, op]


def test_read_map_chain_operator_fusion(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    # Test that a chain of different map operators are fused.
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    map1 = MapRows(read_op, lambda x: x)
    map2 = MapBatches(map1, lambda x: x)
    map3 = FlatMap(map2, lambda x: x)
    map4 = Filter(map3, lambda x: x)
    logical_plan = LogicalPlan(map4, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert map4.name == "Filter(<lambda>)"
    assert (
        physical_op.name == "ReadParquet->Map(<lambda>)->MapBatches(<lambda>)"
        "->FlatMap(<lambda>)->Filter(<lambda>)"
    )
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )
    assert physical_op._logical_operators == [read_op, map1, map2, map3, map4]


def test_read_map_batches_operator_fusion_compatible_remote_args(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that map operators are stilled fused when remote args are compatible.
    compatiple_remote_args_pairs = [
        # Empty remote args are compatible.
        ({}, {}),
        # Test `num_cpus` and `num_gpus`.
        ({"num_cpus": 2}, {"num_cpus": 2}),
        ({"num_gpus": 2}, {"num_gpus": 2}),
        # `num_cpus` defaults to 1, `num_gpus` defaults to 0.
        # The following 2 should be compatible.
        ({"num_cpus": 1}, {}),
        ({}, {"num_gpus": 0}),
        # Test specifying custom resources.
        ({"resources": {"custom": 1}}, {"resources": {"custom": 1}}),
        ({"resources": {"custom": 0}}, {"resources": {}}),
        # If the downstream op doesn't have `scheduling_strategy`, it will
        # inherit from the upstream op.
        ({"scheduling_strategy": "SPREAD"}, {}),
    ]
    for up_remote_args, down_remote_args in compatiple_remote_args_pairs:
        planner = Planner()
        read_op = get_parquet_read_logical_op(
            ray_remote_args={"resources": {"non-existent": 1}},
            parallelism=1,
        )
        op = MapBatches(read_op, lambda x: x, ray_remote_args=up_remote_args)
        op = MapBatches(op, lambda x: x, ray_remote_args=down_remote_args)
        logical_plan = LogicalPlan(op, ctx)

        physical_plan = planner.plan(logical_plan)
        optimized_physical_plan = PhysicalOptimizer().optimize(physical_plan)
        physical_op = optimized_physical_plan.dag

        assert op.name == "MapBatches(<lambda>)", (up_remote_args, down_remote_args)
        assert physical_op.name == "MapBatches(<lambda>)->MapBatches(<lambda>)", (
            up_remote_args,
            down_remote_args,
        )
        assert isinstance(physical_op, MapOperator), (up_remote_args, down_remote_args)
        assert len(physical_op.input_dependencies) == 1, (
            up_remote_args,
            down_remote_args,
        )
        assert physical_op.input_dependencies[0].name == "ReadParquet", (
            up_remote_args,
            down_remote_args,
        )


def test_read_map_batches_operator_fusion_incompatible_remote_args(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that map operators won't get fused if the remote args are incompatible.
    incompatible_remote_args_pairs = [
        # Use different resources.
        ({"num_cpus": 2}, {"num_gpus": 2}),
        # Same resource, but different values.
        ({"num_cpus": 3}, {"num_cpus": 2}),
        # Incompatible custom resources.
        ({"resources": {"custom": 2}}, {"resources": {"custom": 1}}),
        ({"resources": {"custom1": 1}}, {"resources": {"custom2": 1}}),
        # Different scheduling strategies.
        ({"scheduling_strategy": "SPREAD"}, {"scheduling_strategy": "PACK"}),
    ]
    for up_remote_args, down_remote_args in incompatible_remote_args_pairs:
        planner = Planner()
        read_op = get_parquet_read_logical_op(
            ray_remote_args={"resources": {"non-existent": 1}}
        )
        op = MapBatches(read_op, lambda x: x, ray_remote_args=up_remote_args)
        op = MapBatches(op, lambda x: x, ray_remote_args=down_remote_args)
        logical_plan = LogicalPlan(op, ctx)
        physical_plan = planner.plan(logical_plan)
        physical_plan = PhysicalOptimizer().optimize(physical_plan)
        physical_op = physical_plan.dag

        assert op.name == "MapBatches(<lambda>)", (up_remote_args, down_remote_args)
        assert physical_op.name == "MapBatches(<lambda>)", (
            up_remote_args,
            down_remote_args,
        )
        assert isinstance(physical_op, MapOperator), (up_remote_args, down_remote_args)
        assert len(physical_op.input_dependencies) == 1, (
            up_remote_args,
            down_remote_args,
        )
        assert physical_op.input_dependencies[0].name == "MapBatches(<lambda>)", (
            up_remote_args,
            down_remote_args,
        )


def test_read_map_batches_operator_fusion_compute_tasks_to_actors(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that a task-based map operator is fused into an actor-based map operator when
    # the former comes before the latter.
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    op = MapBatches(read_op, lambda x: x)
    op = MapBatches(op, lambda x: x, compute=ray.data.ActorPoolStrategy())
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert op.name == "MapBatches(<lambda>)"
    assert physical_op.name == "ReadParquet->MapBatches(<lambda>)->MapBatches(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)


def test_read_map_batches_operator_fusion_compute_read_to_actors(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that reads fuse into an actor-based map operator.
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    op = MapBatches(read_op, lambda x: x, compute=ray.data.ActorPoolStrategy())
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert op.name == "MapBatches(<lambda>)"
    assert physical_op.name == "ReadParquet->MapBatches(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)


def test_read_map_batches_operator_fusion_incompatible_compute(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that map operators are not fused when compute strategies are incompatible.
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    op = MapBatches(read_op, lambda x: x, compute=ray.data.ActorPoolStrategy())
    op = MapBatches(op, lambda x: x)
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert op.name == "MapBatches(<lambda>)"
    assert physical_op.name == "MapBatches(<lambda>)"
    assert isinstance(physical_op, MapOperator)
    assert len(physical_op.input_dependencies) == 1
    upstream_physical_op = physical_op.input_dependencies[0]
    assert isinstance(upstream_physical_op, MapOperator)
    # Reads should fuse into actor compute.
    assert upstream_physical_op.name == "ReadParquet->MapBatches(<lambda>)"


def test_read_map_batches_operator_fusion_min_rows_per_bundled_input(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test that fusion of map operators merges their block sizes in the expected way
    # (taking the max).
    planner = Planner()
    read_op = get_parquet_read_logical_op(parallelism=1)
    op = MapBatches(read_op, lambda x: x, min_rows_per_bundled_input=2)
    op = MapBatches(op, lambda x: x, min_rows_per_bundled_input=5)
    op = MapBatches(op, lambda x: x, min_rows_per_bundled_input=3)
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)
    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    physical_op = physical_plan.dag

    assert op.name == "MapBatches(<lambda>)"
    # Ops are still fused.
    assert (
        physical_op.name == "ReadParquet->MapBatches(<lambda>)->"
        "MapBatches(<lambda>)->MapBatches(<lambda>)"
    )
    assert isinstance(physical_op, MapOperator)
    # Target block size is set to max.
    assert physical_op._block_ref_bundler._min_rows_per_bundle == 5
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], InputDataBuffer)

    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )


def test_read_map_batches_operator_fusion_with_randomize_blocks_operator(
    ray_start_regular_shared_2_cpus,
):
    # Note: We currently do not fuse MapBatches->RandomizeBlocks.
    # This test is to ensure that we don't accidentally fuse them.
    # There is also an additional optimization rule, under ReorderRandomizeBlocksRule,
    # which collapses RandomizeBlocks operators, so we should not be fusing them
    # to begin with.
    def fn(batch):
        return {"id": [x + 1 for x in batch["id"]]}

    n = 10
    ds = ray.data.range(n)
    ds = ds.randomize_block_order()
    ds = ds.map_batches(fn, batch_size=None)
    assert set(extract_values("id", ds.take_all())) == set(range(1, n + 1))
    assert "ReadRange->MapBatches(fn)->RandomizeBlockOrder" not in ds.stats()
    assert "ReadRange->MapBatches(fn)" in ds.stats()
    _check_usage_record(["ReadRange", "MapBatches", "RandomizeBlockOrder"])


def test_read_map_batches_operator_fusion_with_random_shuffle_operator(
    ray_start_regular_shared_2_cpus, configure_shuffle_method
):
    # Note: we currently only support fusing MapOperator->AllToAllOperator.
    def fn(batch):
        return {"id": [x + 1 for x in batch["id"]]}

    n = 10
    ds = ray.data.range(n)
    ds = ds.map_batches(fn, batch_size=None)
    ds = ds.random_shuffle()
    assert set(extract_values("id", ds.take_all())) == set(range(1, n + 1))
    assert "ReadRange->MapBatches(fn)->RandomShuffle" in ds.stats()
    _check_usage_record(["ReadRange", "MapBatches", "RandomShuffle"])

    ds = ray.data.range(n)
    ds = ds.random_shuffle()
    ds = ds.map_batches(fn, batch_size=None)
    assert set(extract_values("id", ds.take_all())) == set(range(1, n + 1))
    # TODO(Scott): Update below assertion after supporting fusion in
    # the other direction (AllToAllOperator->MapOperator)
    assert "ReadRange->RandomShuffle->MapBatches(fn)" not in ds.stats()
    assert all(op in ds.stats() for op in ("ReadRange", "RandomShuffle", "MapBatches"))
    _check_usage_record(["ReadRange", "RandomShuffle", "MapBatches"])

    # Test fusing multiple `map_batches` with multiple `random_shuffle` operations.
    ds = ray.data.range(n)
    for _ in range(5):
        ds = ds.map_batches(fn, batch_size=None)
    ds = ds.random_shuffle()
    assert set(extract_values("id", ds.take_all())) == set(range(5, n + 5))
    assert f"ReadRange->{'MapBatches(fn)->' * 5}RandomShuffle" in ds.stats()

    # For interweaved map_batches and random_shuffle operations, we expect to fuse the
    # two pairs of MapBatches->RandomShuffle, but not the resulting
    # RandomShuffle operators.
    ds = ray.data.range(n)
    ds = ds.map_batches(fn, batch_size=None)
    ds = ds.random_shuffle()
    ds = ds.map_batches(fn, batch_size=None)
    ds = ds.random_shuffle()
    assert set(extract_values("id", ds.take_all())) == set(range(2, n + 2))
    assert "Operator 1 ReadRange->MapBatches(fn)->RandomShuffle" in ds.stats()
    assert "Operator 2 MapBatches(fn)->RandomShuffle" in ds.stats()
    _check_usage_record(["ReadRange", "RandomShuffle", "MapBatches"])

    # Check the case where the upstream map function returns multiple blocks.
    ctx = ray.data.DataContext.get_current()
    old_target_max_block_size = ctx.target_max_block_size
    ctx.target_max_block_size = 100

    def fn(_):
        return {"data": np.zeros((100, 100))}

    ds = ray.data.range(10)
    ds = ds.repartition(2).map(fn).random_shuffle().materialize()
    assert "Operator 1 ReadRange" in ds.stats()
    assert "Operator 2 Repartition" in ds.stats()
    assert "Operator 3 Map(fn)->RandomShuffle" in ds.stats()
    _check_usage_record(["ReadRange", "RandomShuffle", "Map"])

    ctx.target_max_block_size = old_target_max_block_size


@pytest.mark.parametrize("shuffle", (True, False))
def test_read_map_batches_operator_fusion_with_repartition_operator(
    ray_start_regular_shared_2_cpus, shuffle, configure_shuffle_method
):
    def fn(batch):
        return {"id": [x + 1 for x in batch["id"]]}

    n = 10
    ds = ray.data.range(n)
    ds = ds.map_batches(fn, batch_size=None)
    ds = ds.repartition(2, shuffle=shuffle)
    assert set(extract_values("id", ds.take_all())) == set(range(1, n + 1))

    # Operator fusion is only supported for shuffle repartition.
    if shuffle:
        assert "ReadRange->MapBatches(fn)->Repartition" in ds.stats()
    else:
        assert "ReadRange->MapBatches(fn)->Repartition" not in ds.stats()
        assert "ReadRange->MapBatches(fn)" in ds.stats()
        assert "Repartition" in ds.stats()
    _check_usage_record(["ReadRange", "MapBatches", "Repartition"])


def test_read_map_batches_operator_fusion_with_sort_operator(
    ray_start_regular_shared_2_cpus,
):
    # Note: We currently do not fuse MapBatches->Sort.
    # This test is to ensure that we don't accidentally fuse them, until
    # we implement it later.
    def fn(batch):
        return {"id": [x + 1 for x in batch["id"]]}

    n = 10
    ds = ray.data.range(n)
    ds = ds.map_batches(fn, batch_size=None)
    ds = ds.sort("id")
    assert extract_values("id", ds.take_all()) == list(range(1, n + 1))
    # TODO(Scott): update the below assertions after we support fusion.
    assert "ReadRange->MapBatches->Sort" not in ds.stats()
    assert "ReadRange->MapBatches" in ds.stats()
    assert "Sort" in ds.stats()
    _check_usage_record(["ReadRange", "MapBatches", "Sort"])


def test_read_map_batches_operator_fusion_with_aggregate_operator(
    ray_start_regular_shared_2_cpus, configure_shuffle_method
):
    from ray.data.aggregate import AggregateFn

    # Note: We currently do not fuse MapBatches->Aggregate.
    # This test is to ensure that we don't accidentally fuse them, until
    # we implement it later.
    def fn(batch):
        return {"id": [x % 2 for x in batch["id"]]}

    n = 100
    grouped_ds = ray.data.range(n).map_batches(fn, batch_size=None).groupby("id")
    agg_ds = grouped_ds.aggregate(
        AggregateFn(
            init=lambda k: [0, 0],
            accumulate_row=lambda a, r: [a[0] + r["id"], a[1] + 1],
            merge=lambda a1, a2: [a1[0] + a2[0], a1[1] + a2[1]],
            finalize=lambda a: a[0] / a[1],
            name="foo",
        ),
    )
    agg_ds.take_all() == [{"id": 0, "foo": 0.0}, {"id": 1, "foo": 1.0}]
    # TODO(Scott): update the below assertions after we support fusion.
    assert "ReadRange->MapBatches->Aggregate" not in agg_ds.stats()
    assert "ReadRange->MapBatches" in agg_ds.stats()
    assert "Aggregate" in agg_ds.stats()
    _check_usage_record(["ReadRange", "MapBatches", "Aggregate"])


def test_read_map_chain_operator_fusion_e2e(
    ray_start_regular_shared_2_cpus,
):
    ds = ray.data.range(10, override_num_blocks=2)
    ds = ds.filter(lambda x: x["id"] % 2 == 0)
    ds = ds.map(column_udf("id", lambda x: x + 1))
    ds = ds.map_batches(
        lambda batch: {"id": [2 * x for x in batch["id"]]}, batch_size=None
    )
    ds = ds.flat_map(lambda x: [{"id": -x["id"]}, {"id": x["id"]}])
    assert extract_values("id", ds.take_all()) == [
        -2,
        2,
        -6,
        6,
        -10,
        10,
        -14,
        14,
        -18,
        18,
    ]
    name = (
        "ReadRange->Filter(<lambda>)->Map(<lambda>)"
        "->MapBatches(<lambda>)->FlatMap(<lambda>):"
    )
    assert name in ds.stats()
    _check_usage_record(["ReadRange", "Filter", "Map", "MapBatches", "FlatMap"])


def test_write_fusion(ray_start_regular_shared_2_cpus, tmp_path):
    ds = ray.data.range(10, override_num_blocks=2)
    ds.write_csv(tmp_path)
    assert "ReadRange->Write" in ds._write_ds.stats()
    _check_usage_record(["ReadRange", "WriteCSV"])


@pytest.mark.parametrize(
    "up_use_actor, up_concurrency, down_use_actor, down_concurrency, should_fuse",
    [
        # === Task->Task cases ===
        # Same concurrency set. Should fuse.
        (False, 1, False, 1, True),
        # Different concurrency set. Should not fuse.
        (False, 1, False, 2, False),
        # If one op has concurrency set, and the other doesn't, should not fuse.
        (False, None, False, 1, False),
        (False, 1, False, None, False),
        # === Task->Actor cases ===
        # When Task's concurrency is not set, should fuse.
        (False, None, True, 2, True),
        (False, None, True, (1, 2), True),
        # When max size matches, should fuse.
        (False, 2, True, 2, True),
        (False, 2, True, (1, 2), True),
        # When max size doesn't match, should not fuse.
        (False, 1, True, 2, False),
        (False, 1, True, (1, 2), False),
        # === Actor->Task cases ===
        # Should not fuse whatever concurrency is set.
        (True, 2, False, 2, False),
        # === Actor->Actor cases ===
        # Should not fuse whatever concurrency is set.
        (True, 2, True, 2, False),
    ],
)
def test_map_fusion_with_concurrency_arg(
    ray_start_regular_shared_2_cpus,
    up_use_actor,
    up_concurrency,
    down_use_actor,
    down_concurrency,
    should_fuse,
):
    """Test map operator fusion with different concurrency settings."""

    class Map:
        def __call__(self, row):
            return row

    def map(row):
        return row

    ds = ray.data.range(10, override_num_blocks=2)
    if not up_use_actor:
        ds = ds.map(map, num_cpus=0, concurrency=up_concurrency)
        up_name = "Map(map)"
    else:
        ds = ds.map(Map, num_cpus=0, concurrency=up_concurrency)
        up_name = "Map(Map)"

    if not down_use_actor:
        ds = ds.map(map, num_cpus=0, concurrency=down_concurrency)
        down_name = "Map(map)"
    else:
        ds = ds.map(Map, num_cpus=0, concurrency=down_concurrency)
        down_name = "Map(Map)"

    assert extract_values("id", ds.take_all()) == list(range(10))

    name = f"{up_name}->{down_name}"
    stats = ds.stats()
    if should_fuse:
        assert name in stats, stats
    else:
        assert name not in stats, stats


def test_write_operator(ray_start_regular_shared_2_cpus, tmp_path):
    ctx = DataContext.get_current()

    concurrency = 2
    planner = Planner()
    datasink = ParquetDatasink(tmp_path)
    read_op = get_parquet_read_logical_op()
    op = Write(
        read_op,
        datasink,
        concurrency=concurrency,
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Write"
    assert isinstance(physical_op, TaskPoolMapOperator)
    assert physical_op._concurrency == concurrency
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


def test_sort_operator(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = Sort(
        read_op,
        sort_key=SortKey("col1"),
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Sort"
    assert isinstance(physical_op, AllToAllOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_shuffle_max_block_size
    )


def test_sort_e2e(ray_start_regular_shared_2_cpus, configure_shuffle_method, tmp_path):
    ds = ray.data.range(100, override_num_blocks=4)
    ds = ds.random_shuffle()
    ds = ds.sort("id")
    assert extract_values("id", ds.take_all()) == list(range(100))
    _check_usage_record(["ReadRange", "RandomShuffle", "Sort"])

    df = pd.DataFrame({"one": list(range(100)), "two": ["a"] * 100})
    ds = ray.data.from_pandas([df])
    ds.write_parquet(tmp_path)

    ds = ray.data.read_parquet(tmp_path)
    ds = ds.random_shuffle()
    ds1 = ds.sort("one")
    ds2 = ds.sort("one", descending=True)
    r1 = ds1.select_columns(["one"]).take_all()
    r2 = ds2.select_columns(["one"]).take_all()
    assert [d["one"] for d in r1] == list(range(100))
    assert [d["one"] for d in r2] == list(reversed(range(100)))


def test_sort_validate_keys(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(10)
    assert extract_values("id", ds.sort("id").take_all()) == list(range(10))

    invalid_col_name = "invalid_column"
    with pytest.raises(ValueError, match="there's no such column in the dataset"):
        ds.sort(invalid_col_name).take_all()

    ds_named = ray.data.from_items(
        [
            {"col1": 1, "col2": 2},
            {"col1": 3, "col2": 4},
            {"col1": 5, "col2": 6},
            {"col1": 7, "col2": 8},
        ]
    )

    ds_sorted_col1 = ds_named.sort("col1", descending=True)
    r1 = ds_sorted_col1.select_columns(["col1"]).take_all()
    r2 = ds_sorted_col1.select_columns(["col2"]).take_all()
    assert [d["col1"] for d in r1] == [7, 5, 3, 1]
    assert [d["col2"] for d in r2] == [8, 6, 4, 2]

    with pytest.raises(ValueError, match="there's no such column in the dataset"):
        ds_named.sort(invalid_col_name).take_all()


def test_inherit_batch_format_rule():
    from ray.data._internal.logical.rules.inherit_batch_format import (
        InheritBatchFormatRule,
    )

    ctx = DataContext.get_current()

    operator1 = get_parquet_read_logical_op()
    operator2 = MapBatches(operator1, fn=lambda g: g, batch_format="pandas")
    sort_key = SortKey("number", descending=True)
    operator3 = Sort(operator2, sort_key)
    original_plan = LogicalPlan(dag=operator3, context=ctx)

    rule = InheritBatchFormatRule()
    optimized_plan = rule.apply(original_plan)
    assert optimized_plan.dag._batch_format == "pandas"


def test_batch_format_on_sort(ray_start_regular_shared_2_cpus):
    """Checks that the Sort op can inherit batch_format from upstream ops correctly."""
    ds = ray.data.from_items(
        [
            {"col1": 1, "col2": 2},
            {"col1": 1, "col2": 4},
            {"col1": 5, "col2": 6},
            {"col1": 7, "col2": 8},
        ]
    )
    df_expected = pd.DataFrame(
        {
            "col1": [7, 5, 1, 1],
            "col2": [8, 6, 4, 2],
        }
    )
    df_actual = (
        ds.groupby("col1")
        .map_groups(lambda g: g, batch_format="pandas")
        .sort("col2", descending=True)
        .to_pandas()
    )
    pd.testing.assert_frame_equal(df_actual, df_expected)


def test_batch_format_on_aggregate(ray_start_regular_shared_2_cpus):
    """Checks that the Aggregate op can inherit batch_format
    from upstream ops correctly."""
    from ray.data.aggregate import AggregateFn

    ds = ray.data.from_items(
        [
            {"col1": 1, "col2": 2},
            {"col1": 1, "col2": 4},
            {"col1": 5, "col2": 6},
            {"col1": 7, "col2": 8},
        ]
    )
    aggregation = AggregateFn(
        init=lambda column: 1,
        accumulate_row=lambda a, row: a * row["col2"],
        merge=lambda a1, a2: a1 * a2,
        name="prod",
    )
    assert (
        ds.groupby("col1")
        .map_groups(lambda g: g, batch_format="pandas")
        .aggregate(aggregation)
    ) == {"prod": 384}


def test_aggregate_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = Aggregate(
        read_op,
        key="col1",
        aggs=[Count()],
    )
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Aggregate"
    assert isinstance(physical_op, AllToAllOperator)
    assert len(physical_op.input_dependencies) == 1
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_shuffle_max_block_size
    )

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


def test_aggregate_e2e(ray_start_regular_shared_2_cpus, configure_shuffle_method):
    ds = ray.data.range(100, override_num_blocks=4)
    ds = ds.groupby("id").count()
    assert ds.count() == 100
    for idx, row in enumerate(ds.sort("id").iter_rows()):
        assert row == {"id": idx, "count()": 1}
    _check_usage_record(["ReadRange", "Aggregate"])


def test_aggregate_validate_keys(ray_start_regular_shared_2_cpus):
    ds = ray.data.range(10)
    invalid_col_name = "invalid_column"
    with pytest.raises(ValueError):
        ds.groupby(invalid_col_name).count()

    ds_named = ray.data.from_items(
        [
            {"col1": 1, "col2": "a"},
            {"col1": 1, "col2": "b"},
            {"col1": 2, "col2": "c"},
            {"col1": 3, "col2": "c"},
        ]
    )

    ds_groupby_col1 = ds_named.groupby("col1").count()
    assert ds_groupby_col1.take_all() == [
        {"col1": 1, "count()": 2},
        {"col1": 2, "count()": 1},
        {"col1": 3, "count()": 1},
    ]
    ds_groupby_col2 = ds_named.groupby("col2").count()
    assert ds_groupby_col2.take_all() == [
        {"col2": "a", "count()": 1},
        {"col2": "b", "count()": 1},
        {"col2": "c", "count()": 2},
    ]

    with pytest.raises(
        ValueError,
        match="there's no such column in the dataset",
    ):
        ds_named.groupby(invalid_col_name).count()


def test_zip_operator(ray_start_regular_shared_2_cpus):
    ctx = DataContext.get_current()

    planner = Planner()
    read_op1 = get_parquet_read_logical_op()
    read_op2 = get_parquet_read_logical_op()
    op = Zip(read_op1, read_op2)
    plan = LogicalPlan(op, ctx)
    physical_op = planner.plan(plan).dag

    assert op.name == "Zip"
    assert isinstance(physical_op, ZipOperator)
    assert len(physical_op.input_dependencies) == 2
    assert isinstance(physical_op.input_dependencies[0], MapOperator)
    assert isinstance(physical_op.input_dependencies[1], MapOperator)

    assert (
        physical_op.actual_target_max_block_size
        == DataContext.get_current().target_max_block_size
    )

    # Check that the linked logical operator is the same the input op.
    assert physical_op._logical_operators == [op]


@pytest.mark.parametrize(
    "num_blocks1,num_blocks2",
    list(itertools.combinations_with_replacement(range(1, 12), 2)),
)
def test_zip_e2e(ray_start_regular_shared_2_cpus, num_blocks1, num_blocks2):
    n = 12
    ds1 = ray.data.range(n, override_num_blocks=num_blocks1)
    ds2 = ray.data.range(n, override_num_blocks=num_blocks2).map(
        column_udf("id", lambda x: x + 1)
    )
    ds = ds1.zip(ds2)
    assert ds.take() == named_values(["id", "id_1"], zip(range(n), range(1, n + 1)))
    _check_usage_record(["ReadRange", "Zip"])


def test_from_dask_e2e(ray_start_regular_shared_2_cpus):
    import dask.dataframe as dd

    df = pd.DataFrame({"one": list(range(100)), "two": list(range(100))})
    ddf = dd.from_pandas(df, npartitions=10)
    ds = ray.data.from_dask(ddf)
    # `ds.take_all()` triggers execution with new backend, which is
    # needed for checking operator usage below.
    assert len(ds.take_all()) == len(df)
    dfds = ds.to_pandas()
    assert df.equals(dfds)

    # Underlying implementation uses `FromPandas` operator
    assert "FromPandas" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromPandas"
    _check_usage_record(["FromPandas"])


def test_from_modin_e2e(ray_start_regular_shared_2_cpus):
    import modin.pandas as mopd

    df = pd.DataFrame(
        {"one": list(range(100)), "two": list(range(100))},
    )
    modf = mopd.DataFrame(df)
    ds = ray.data.from_modin(modf)
    # `ds.take_all()` triggers execution with new backend, which is
    # needed for checking operator usage below.
    assert len(ds.take_all()) == len(df)
    # `ds.to_pandas()` does not use the new backend.
    dfds = ds.to_pandas()

    assert df.equals(dfds)
    # Check that metadata fetch is included in stats. This is `FromPandas`
    # instead of `FromModin` because `from_modin` reduces to `from_pandas_refs`.
    assert "FromPandas" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromPandas"
    _check_usage_record(["FromPandas"])


@pytest.mark.parametrize("enable_pandas_block", [False, True])
def test_from_pandas_refs_e2e(ray_start_regular_shared_2_cpus, enable_pandas_block):
    ctx = ray.data.context.DataContext.get_current()
    old_enable_pandas_block = ctx.enable_pandas_block
    ctx.enable_pandas_block = enable_pandas_block

    try:
        df1 = pd.DataFrame({"one": [1, 2, 3], "two": ["a", "b", "c"]})
        df2 = pd.DataFrame({"one": [4, 5, 6], "two": ["e", "f", "g"]})

        ds = ray.data.from_pandas_refs([ray.put(df1), ray.put(df2)])
        values = [(r["one"], r["two"]) for r in ds.take(6)]
        rows = [(r.one, r.two) for _, r in pd.concat([df1, df2]).iterrows()]
        assert values == rows
        # Check that metadata fetch is included in stats.
        assert "FromPandas" in ds.stats()
        assert ds._plan._logical_plan.dag.name == "FromPandas"

        # Test chaining multiple operations
        ds2 = ds.map_batches(lambda x: x)
        values = [(r["one"], r["two"]) for r in ds2.take(6)]
        assert values == rows
        assert "MapBatches" in ds2.stats()
        assert "FromPandas" in ds2.stats()
        assert ds2._plan._logical_plan.dag.name == "MapBatches(<lambda>)"

        # test from single pandas dataframe
        ds = ray.data.from_pandas_refs(ray.put(df1))
        values = [(r["one"], r["two"]) for r in ds.take(3)]
        rows = [(r.one, r.two) for _, r in df1.iterrows()]
        assert values == rows
        # Check that metadata fetch is included in stats.
        assert "FromPandas" in ds.stats()
        assert ds._plan._logical_plan.dag.name == "FromPandas"
        _check_usage_record(["FromPandas"])
    finally:
        ctx.enable_pandas_block = old_enable_pandas_block


def test_from_numpy_refs_e2e(ray_start_regular_shared_2_cpus):
    import numpy as np

    arr1 = np.expand_dims(np.arange(0, 4), axis=1)
    arr2 = np.expand_dims(np.arange(4, 8), axis=1)

    ds = ray.data.from_numpy_refs([ray.put(arr1), ray.put(arr2)])
    values = np.stack(extract_values("data", ds.take(8)))
    np.testing.assert_array_equal(values, np.concatenate((arr1, arr2)))
    # Check that conversion task is included in stats.
    assert "FromNumpy" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromNumpy"
    _check_usage_record(["FromNumpy"])

    # Test chaining multiple operations
    ds2 = ds.map_batches(lambda x: x)
    values = np.stack(extract_values("data", ds2.take(8)))
    np.testing.assert_array_equal(values, np.concatenate((arr1, arr2)))
    assert "MapBatches" in ds2.stats()
    assert "FromNumpy" in ds2.stats()
    assert ds2._plan._logical_plan.dag.name == "MapBatches(<lambda>)"
    _check_usage_record(["FromNumpy", "MapBatches"])

    # Test from single NumPy ndarray.
    ds = ray.data.from_numpy_refs(ray.put(arr1))
    values = np.stack(extract_values("data", ds.take(4)))
    np.testing.assert_array_equal(values, arr1)
    # Check that conversion task is included in stats.
    assert "FromNumpy" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromNumpy"
    _check_usage_record(["FromNumpy"])


def test_from_arrow_refs_e2e(ray_start_regular_shared_2_cpus):
    import pyarrow as pa

    df1 = pd.DataFrame({"one": [1, 2, 3], "two": ["a", "b", "c"]})
    df2 = pd.DataFrame({"one": [4, 5, 6], "two": ["e", "f", "g"]})
    ds = ray.data.from_arrow_refs(
        [ray.put(pa.Table.from_pandas(df1)), ray.put(pa.Table.from_pandas(df2))]
    )

    values = [(r["one"], r["two"]) for r in ds.take(6)]
    rows = [(r.one, r.two) for _, r in pd.concat([df1, df2]).iterrows()]
    assert values == rows
    # Check that metadata fetch is included in stats.
    assert "FromArrow" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromArrow"
    _check_usage_record(["FromArrow"])

    # test from single pyarrow table ref
    ds = ray.data.from_arrow_refs(ray.put(pa.Table.from_pandas(df1)))
    values = [(r["one"], r["two"]) for r in ds.take(3)]
    rows = [(r.one, r.two) for _, r in df1.iterrows()]
    assert values == rows
    # Check that conversion task is included in stats.
    assert "FromArrow" in ds.stats()
    assert ds._plan._logical_plan.dag.name == "FromArrow"
    _check_usage_record(["FromArrow"])


def test_from_huggingface_e2e(ray_start_regular_shared_2_cpus):
    import datasets

    from ray.data.tests.test_huggingface import hfds_assert_equals

    data = datasets.load_dataset("tweet_eval", "emotion")
    assert isinstance(data, datasets.DatasetDict)
    ray_datasets = {
        "train": ray.data.from_huggingface(data["train"]),
        "validation": ray.data.from_huggingface(data["validation"]),
        "test": ray.data.from_huggingface(data["test"]),
    }

    for ds_key, ds in ray_datasets.items():
        assert isinstance(ds, ray.data.Dataset)
        # `ds.take_all()` triggers execution with new backend, which is
        # needed for checking operator usage below.
        assert len(ds.take_all()) > 0
        # Check that metadata fetch is included in stats;
        # the underlying implementation uses the `ReadParquet` operator
        # as this is an un-transformed public dataset.
        assert "ReadParquet" in ds.stats() or "FromArrow" in ds.stats()
        assert (
            ds._plan._logical_plan.dag.name == "ReadParquet"
            or ds._plan._logical_plan.dag.name == "FromArrow"
        )
        # use sort by 'text' to match order of rows
        hfds_assert_equals(data[ds_key], ds)
        try:
            _check_usage_record(["ReadParquet"])
        except AssertionError:
            _check_usage_record(["FromArrow"])

    # test transformed public dataset for fallback behavior
    base_hf_dataset = data["train"]
    hf_dataset_split = base_hf_dataset.train_test_split(test_size=0.2)
    ray_dataset_split_train = ray.data.from_huggingface(hf_dataset_split["train"])
    assert isinstance(ray_dataset_split_train, ray.data.Dataset)
    # `ds.take_all()` triggers execution with new backend, which is
    # needed for checking operator usage below.
    assert len(ray_dataset_split_train.take_all()) > 0
    # Check that metadata fetch is included in stats;
    # the underlying implementation uses the `FromArrow` operator.
    assert "FromArrow" in ray_dataset_split_train.stats()
    assert ray_dataset_split_train._plan._logical_plan.dag.name == "FromArrow"
    assert ray_dataset_split_train.count() == hf_dataset_split["train"].num_rows
    _check_usage_record(["FromArrow"])


@pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason="Skip due to incompatibility tensorflow with Python 3.12+",
)
def test_from_tf_e2e(ray_start_regular_shared_2_cpus):
    import tensorflow as tf
    import tensorflow_datasets as tfds

    tf_dataset = tfds.load("mnist", split=["train"], as_supervised=True)[0]
    tf_dataset = tf_dataset.take(8)  # Use subset to make test run faster.

    ray_dataset = ray.data.from_tf(tf_dataset)

    actual_data = extract_values("item", ray_dataset.take_all())
    expected_data = list(tf_dataset)
    assert len(actual_data) == len(expected_data)
    for (expected_features, expected_label), (actual_features, actual_label) in zip(
        expected_data, actual_data
    ):
        tf.debugging.assert_equal(expected_features, actual_features)
        tf.debugging.assert_equal(expected_label, actual_label)

    # Check that metadata fetch is included in stats.
    assert "FromItems" in ray_dataset.stats()
    # Underlying implementation uses `FromItems` operator
    assert ray_dataset._plan._logical_plan.dag.name == "FromItems"
    _check_usage_record(["FromItems"])


def test_from_torch_e2e(ray_start_regular_shared_2_cpus, tmp_path):
    import torchvision

    torch_dataset = torchvision.datasets.FashionMNIST(tmp_path, download=True)

    ray_dataset = ray.data.from_torch(torch_dataset)

    expected_data = list(torch_dataset)
    actual_data = list(ray_dataset.take_all())
    assert extract_values("item", actual_data) == expected_data

    # Check that metadata fetch is included in stats.
    assert "ReadTorch" in ray_dataset.stats()

    # Underlying implementation uses `FromItems` operator
    assert ray_dataset._plan._logical_plan.dag.name == "ReadTorch"
    _check_usage_record(["ReadTorch"])


@pytest.mark.skip(
    reason="Limit pushdown currently disabled, see "
    "https://github.com/ray-project/ray/issues/36295"
)
def test_limit_pushdown(ray_start_regular_shared_2_cpus):
    def f1(x):
        return x

    def f2(x):
        return x

    # Test basic limit pushdown past Map.
    ds = ray.data.range(100, override_num_blocks=100).map(f1).limit(1)
    _check_valid_plan_and_result(
        ds, "Read[ReadRange] -> Limit[limit=1] -> MapRows[Map(f1)]", [{"id": 0}]
    )

    # Test basic Limit -> Limit fusion.
    ds2 = ray.data.range(100).limit(5).limit(100)
    _check_valid_plan_and_result(
        ds2, "Read[ReadRange] -> Limit[limit=5]", [{"id": i} for i in range(5)]
    )

    ds2 = ray.data.range(100).limit(100).limit(5)
    _check_valid_plan_and_result(
        ds2, "Read[ReadRange] -> Limit[limit=5]", [{"id": i} for i in range(5)]
    )

    ds2 = ray.data.range(100).limit(50).limit(80).limit(5).limit(20)
    _check_valid_plan_and_result(
        ds2, "Read[ReadRange] -> Limit[limit=5]", [{"id": i} for i in range(5)]
    )

    # Test limit pushdown and Limit -> Limit fusion together.
    ds3 = ray.data.range(100).limit(5).map(f1).limit(100)
    _check_valid_plan_and_result(
        ds3,
        "Read[ReadRange] -> Limit[limit=5] -> MapRows[Map(f1)]",
        [{"id": i} for i in range(5)],
    )

    ds3 = ray.data.range(100).limit(100).map(f1).limit(5)
    _check_valid_plan_and_result(
        ds3,
        "Read[ReadRange] -> Limit[limit=5] -> MapRows[Map(f1)]",
        [{"id": i} for i in range(5)],
    )

    # Test basic limit pushdown up to Sort.
    ds4 = ray.data.range(100).sort("id").limit(5)
    _check_valid_plan_and_result(
        ds4,
        "Read[ReadRange] -> Sort[Sort] -> Limit[limit=5]",
        [{"id": i} for i in range(5)],
    )

    ds4 = ray.data.range(100).sort("id").map(f1).limit(5)
    _check_valid_plan_and_result(
        ds4,
        "Read[ReadRange] -> Sort[Sort] -> Limit[limit=5] -> MapRows[Map(f1)]",
        [{"id": i} for i in range(5)],
    )
    # Test limit pushdown between two Map operators.
    ds5 = ray.data.range(100, override_num_blocks=100).map(f1).limit(1).map(f2)
    # Limit operators get pushed down in the logical plan optimization,
    # then fused together.
    _check_valid_plan_and_result(
        ds5,
        "Read[ReadRange] -> Limit[limit=1] -> MapRows[Map(f1)] -> MapRows[Map(f2)]",
        [{"id": 0}],
    )
    # Map operators only get fused in the optimized physical plan, not the logical plan.
    assert "Map(f1)->Map(f2)" in ds5.stats()

    # More complex interweaved case.
    ds6 = ray.data.range(100).sort("id").map(f1).limit(20).sort("id").map(f2).limit(5)
    _check_valid_plan_and_result(
        ds6,
        "Read[ReadRange] -> Sort[Sort] -> Limit[limit=20] -> MapRows[Map(f1)] -> "
        "Sort[Sort] -> Limit[limit=5] -> MapRows[Map(f2)]",
        [{"id": i} for i in range(5)],
    )


def test_execute_to_legacy_block_list(
    ray_start_regular_shared_2_cpus,
):
    ds = ray.data.range(10)
    # Stats not initialized until `ds.iter_rows()` is called
    assert ds._plan._snapshot_stats is None

    for i, row in enumerate(ds.iter_rows()):
        assert row["id"] == i

    assert ds._plan._snapshot_stats is not None
    assert "ReadRange" in ds._plan._snapshot_stats.metadata
    assert ds._plan._snapshot_stats.time_total_s > 0


def test_streaming_executor(
    ray_start_regular_shared_2_cpus,
):
    ds = ray.data.range(100, override_num_blocks=4)
    ds = ds.map_batches(lambda x: x)
    ds = ds.filter(lambda x: x["id"] > 0)
    ds = ds.random_shuffle()
    ds = ds.map_batches(lambda x: x)

    result = []
    for batch in ds.iter_batches(batch_size=3):
        batch = batch["id"]
        assert len(batch) == 3, batch
        result.extend(batch)
    assert sorted(result) == list(range(1, 100)), result
    _check_usage_record(["ReadRange", "MapBatches", "Filter", "RandomShuffle"])


def test_schema_partial_execution(
    ray_start_regular_shared_2_cpus,
):
    fields = [
        ("sepal.length", pa.float64()),
        ("sepal.width", pa.float64()),
        ("petal.length", pa.float64()),
        ("petal.width", pa.float64()),
        ("variety", pa.string()),
    ]
    ds = ray.data.read_parquet(
        "example://iris.parquet",
        schema=pa.schema(fields),
        override_num_blocks=2,
    ).map_batches(lambda x: x)

    iris_schema = ds.schema()
    assert iris_schema == ray.data.dataset.Schema(pa.schema(fields))
    # Verify that ds.schema() executes only the first block, and not the
    # entire Dataset.
    assert not ds._plan.has_computed_output()
    assert ds._plan._logical_plan.dag.dag_str == (
        "Read[ReadParquet] -> MapBatches[MapBatches(<lambda>)]"
    )


def check_transform_fns(op, expected_types):
    assert isinstance(op, MapOperator)
    transform_fns = op.get_map_transformer().get_transform_fns()
    assert len(transform_fns) == len(expected_types), transform_fns
    for i, transform_fn in enumerate(transform_fns):
        assert isinstance(transform_fn, expected_types[i]), transform_fn


@pytest.mark.skip("Needs zero-copy optimization for read->map_batches.")
def test_zero_copy_fusion_eliminate_build_output_blocks(
    ray_start_regular_shared_2_cpus,
):
    ctx = DataContext.get_current()

    # Test the EliminateBuildOutputBlocks optimization rule.
    planner = Planner()
    read_op = get_parquet_read_logical_op()
    op = MapBatches(read_op, lambda x: x)
    logical_plan = LogicalPlan(op, ctx)
    physical_plan = planner.plan(logical_plan)

    # Before optimization, there should be a map op and and read op.
    # And they should have the following transform_fns.
    map_op = physical_plan.dag
    check_transform_fns(
        map_op,
        [
            BlocksToBatchesMapTransformFn,
            BatchMapTransformFn,
            BuildOutputBlocksMapTransformFn,
        ],
    )
    read_op = map_op.input_dependencies[0]
    check_transform_fns(
        read_op,
        [
            BlockMapTransformFn,
            BuildOutputBlocksMapTransformFn,
        ],
    )

    physical_plan = PhysicalOptimizer().optimize(physical_plan)
    fused_op = physical_plan.dag

    # After optimization, read and map ops should be fused as one op.
    # And the BuidlOutputBlocksMapTransformFn in the middle should be dropped.
    check_transform_fns(
        fused_op,
        [
            BlockMapTransformFn,
            BlocksToBatchesMapTransformFn,
            BatchMapTransformFn,
            BuildOutputBlocksMapTransformFn,
        ],
    )


@pytest.mark.parametrize(
    "average_bytes_per_output, ray_remote_args, ray_remote_args_fn, data_context, expected_memory",
    [
        # The user hasn't set memory, so the rule should configure it.
        (1, None, None, DataContext(), 1),
        # The user has set memory, so the rule shouldn't change it.
        (1, {"memory": 2}, None, DataContext(), 2),
        (1, None, lambda: {"memory": 2}, DataContext(), 2),
        # An estimate isn't available, so the rule shouldn't configure memory.
        (None, None, None, DataContext(), None),
    ],
)
def test_configure_map_task_memory_rule(
    average_bytes_per_output,
    ray_remote_args,
    ray_remote_args_fn,
    data_context,
    expected_memory,
):
    input_op = InputDataBuffer(MagicMock(), [])
    map_op = MapOperator.create(
        MagicMock(),
        input_op=input_op,
        data_context=data_context,
        ray_remote_args=ray_remote_args,
        ray_remote_args_fn=ray_remote_args_fn,
    )
    map_op._metrics = MagicMock(
        spec=OpRuntimeMetrics, average_bytes_per_output=average_bytes_per_output
    )
    plan = PhysicalPlan(map_op, op_map=MagicMock(), context=data_context)
    rule = ConfigureMapTaskMemoryUsingOutputSize()

    new_plan = rule.apply(plan)

    remote_args = new_plan.dag._get_runtime_ray_remote_args()
    assert remote_args.get("memory") == expected_memory


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

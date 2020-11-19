import datetime
import os
import typing

import pandas
import pytest

import flytekit
from flytekit import ContainerTask, SQLTask, TaskReference, WorkflowReference, dynamic, kwtypes, maptask
from flytekit.annotated import context_manager, launch_plan, promise
from flytekit.annotated.condition import conditional
from flytekit.annotated.context_manager import ExecutionState, Image, ImageConfig
from flytekit.annotated.promise import Promise
from flytekit.annotated.task import metadata, task
from flytekit.annotated.testing import patch, task_mock
from flytekit.annotated.type_engine import RestrictedTypeError, TypeEngine
from flytekit.annotated.workflow import workflow
from flytekit.common.nodes import SdkNode
from flytekit.common.promise import NodeOutput
from flytekit.interfaces.data.data_proxy import FileAccessProvider
from flytekit.models.core import types as _core_types
from flytekit.models.interface import Parameter
from flytekit.models.types import LiteralType, SimpleType
from flytekit.taskplugins.spark import Spark
from flytekit.types.flyte_file import FlyteFile
from flytekit.types.schema import FlyteSchema, SchemaOpenMode


def test_default_wf_params_works():
    @task
    def my_task(a: int):
        wf_params = flytekit.current_context()
        assert wf_params.execution_id == "ex:local:local:local"

    my_task(a=3)


def test_simple_input_output():
    @task
    def my_task(a: int) -> typing.NamedTuple("OutputsBC", b=int, c=str):
        ctx = flytekit.current_context()
        assert ctx.execution_id == "ex:local:local:local"
        return a + 2, "hello world"

    assert my_task(a=3) == (5, "hello world")


def test_simple_input_no_output():
    @task
    def my_task(a: int):
        pass

    assert my_task(a=3) is None

    ctx = context_manager.FlyteContext.current_context()
    with ctx.new_compilation_context() as ctx:
        outputs = my_task(a=3)
        assert outputs is None


def test_single_output():
    @task
    def my_task() -> str:
        return "Hello world"

    assert my_task() == "Hello world"

    ctx = context_manager.FlyteContext.current_context()
    with ctx.new_compilation_context() as ctx:
        outputs = my_task()
        assert ctx.compilation_state is not None
        nodes = ctx.compilation_state.nodes
        assert len(nodes) == 1
        assert outputs.is_ready is False
        assert outputs.ref.sdk_node is nodes[0]


def test_engine_file_output():
    basic_blob_type = _core_types.BlobType(format="", dimensionality=_core_types.BlobType.BlobDimensionality.SINGLE,)

    fs = FileAccessProvider(local_sandbox_dir="/tmp/flytetesting")
    with context_manager.FlyteContext.current_context().new_file_access_context(file_access_provider=fs) as ctx:
        # Write some text to a file not in that directory above
        test_file_location = "/tmp/sample.txt"
        with open(test_file_location, "w") as fh:
            fh.write("Hello World\n")

        lit = TypeEngine.to_literal(ctx, test_file_location, os.PathLike, LiteralType(blob=basic_blob_type))

        # Since we're using local as remote, we should be able to just read the file from the 'remote' location.
        with open(lit.scalar.blob.uri, "r") as fh:
            assert fh.readline() == "Hello World\n"

        # We should also be able to turn the thing back into regular python native thing.
        redownloaded_local_file_location = TypeEngine.to_python_value(ctx, lit, os.PathLike)
        with open(redownloaded_local_file_location, "r") as fh:
            assert fh.readline() == "Hello World\n"


def test_wf1():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = t2(a=y, b=b)
        return x, d

    assert len(my_wf._nodes) == 2
    assert my_wf._nodes[0].id == "node-0"
    assert my_wf._nodes[1]._upstream_nodes[0] is my_wf._nodes[0]

    assert len(my_wf._output_bindings) == 2
    assert my_wf._output_bindings[0].var == "out_0"
    assert my_wf._output_bindings[0].binding.promise.var == "t1_int_output"


def test_wf1_run():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = t2(a=y, b=b)
        return x, d

    x = my_wf(a=5, b="hello ")
    assert x == (7, "hello world")


def test_wf1_with_overrides():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a).with_overrides(name="x")
        d = t2(a=y, b=b).with_overrides()
        return x, d

    x = my_wf(a=5, b="hello ")
    assert x == (7, "hello world")


def test_wf1_with_list_of_inputs():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: typing.List[str]) -> str:
        return " ".join(a)

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = t2(a=[b, y])
        return x, d

    x = my_wf(a=5, b="hello")
    assert x == (7, "hello world")

    @workflow
    def my_wf2(a: int, b: str) -> int:
        x, y = t1(a=a)
        t2(a=[b, y])
        return x

    x = my_wf2(a=5, b="hello")
    assert x == 7


def test_wf_output_mismatch():
    with pytest.raises(AssertionError):

        @workflow
        def my_wf(a: int, b: str) -> (int, str):
            return a

    with pytest.raises(AssertionError):

        @workflow
        def my_wf2(a: int, b: str) -> int:
            return a, b

    @workflow
    def my_wf3(a: int, b: str) -> int:
        return (a,)

    my_wf3(a=10, b="hello")


def test_promise_return():
    """
    Testing that when a workflow is local executed but a local wf execution context already exists, Promise objects
    are returned wrapping Flyte literals instead of the unpacked dict.
    """

    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @workflow
    def mimic_sub_wf(a: int) -> (str, str):
        x, y = t1(a=a)
        u, v = t1(a=x)
        return y, v

    ctx = context_manager.FlyteContext.current_context()

    with ctx.new_execution_context(mode=ExecutionState.Mode.LOCAL_WORKFLOW_EXECUTION) as ctx:
        a, b = mimic_sub_wf(a=3)

    assert isinstance(a, promise.Promise)
    assert isinstance(b, promise.Promise)
    assert a.val.scalar.value.string_value == "world-5"
    assert b.val.scalar.value.string_value == "world-7"


def test_wf1_with_subwf():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_subwf(a: int) -> (str, str):
        x, y = t1(a=a)
        u, v = t1(a=x)
        return y, v

    @workflow
    def my_wf(a: int, b: str) -> (int, str, str):
        x, y = t1(a=a).with_overrides()
        u, v = my_subwf(a=x)
        return x, u, v

    x = my_wf(a=5, b="hello ")
    assert x == (7, "world-9", "world-11")


def test_wf1_with_sql():
    sql = SQLTask(
        "my-query",
        query_template="SELECT * FROM hive.city.fact_airport_sessions WHERE ds = '{{ .Inputs.ds }}' LIMIT 10",
        inputs=kwtypes(ds=datetime.datetime),
        metadata=metadata(retries=2),
    )

    @task
    def t1() -> datetime.datetime:
        return datetime.datetime.now()

    @workflow
    def my_wf() -> str:
        dt = t1()
        return sql(ds=dt)

    with task_mock(sql) as mock:
        mock.return_value = "Hello"
        assert my_wf() == "Hello"


def test_wf1_with_sql_with_patch():
    sql = SQLTask(
        "my-query",
        query_template="SELECT * FROM hive.city.fact_airport_sessions WHERE ds = '{{ .Inputs.ds }}' LIMIT 10",
        inputs=kwtypes(ds=datetime.datetime),
        metadata=metadata(retries=2),
    )

    @task
    def t1() -> datetime.datetime:
        return datetime.datetime.now()

    @workflow
    def my_wf() -> str:
        dt = t1()
        return sql(ds=dt)

    @patch(sql)
    def test_user_demo_test(mock_sql):
        mock_sql.return_value = "Hello"
        assert my_wf() == "Hello"

    # Have to call because tests inside tests don't run
    test_user_demo_test()


def test_wf1_with_spark():
    @task(task_config=Spark())
    def my_spark(spark_session, a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = my_spark(a=a)
        d = t2(a=y, b=b)
        return x, d

    x = my_wf(a=5, b="hello ")
    assert x == (7, "hello world")


def test_wf1_with_map():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @task
    def t2(a: typing.List[int], b: typing.List[str]) -> (int, str):
        ra = 0
        for x in a:
            ra += x
        rb = ""
        for x in b:
            rb += x
        return ra, rb

    @workflow
    def my_wf(a: typing.List[int]) -> (int, str):
        x, y = maptask(t1, metadata=metadata(retries=1))(a=a)
        return t2(a=x, b=y)

    x = my_wf(a=[5, 6])
    assert x == (15, "world-7world-8")


def test_wf1_compile_time_constant_vars():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = t2(a="This is my way", b=b)
        return x, d

    x = my_wf(a=5, b="hello ")
    assert x == (7, "hello This is my way")


def test_wf1_with_constant_return():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        t2(a="This is my way", b=b)
        return x, "A constant output"

    x = my_wf(a=5, b="hello ")
    assert x == (7, "A constant output")

    @workflow
    def my_wf2(a: int, b: str) -> int:
        t1(a=a)
        t2(a="This is my way", b=b)
        return 10

    assert my_wf2(a=5, b="hello ") == 10


def test_wf1_with_dynamic():
    @task
    def t1(a: int) -> str:
        a = a + 2
        return "world-" + str(a)

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @dynamic
    def my_subwf(a: int) -> typing.List[str]:
        s = []
        for i in range(a):
            s.append(t1(a=i))
        return s

    @workflow
    def my_wf(a: int, b: str) -> (str, typing.List[str]):
        x = t2(a=b, b=b)
        v = my_subwf(a=a)
        return x, v

    v = 5
    x = my_wf(a=v, b="hello ")
    assert x == ("hello hello ", ["world-" + str(i) for i in range(2, v + 2)])

    with context_manager.FlyteContext.current_context().new_registration_settings(
        registration_settings=context_manager.RegistrationSettings(
            project="test_proj",
            domain="test_domain",
            version="abc",
            image_config=ImageConfig(Image(name="name", fqn="image", tag="name")),
            env={},
        )
    ) as ctx:
        with ctx.new_execution_context(mode=ExecutionState.Mode.TASK_EXECUTION) as ctx:
            dynamic_job_spec = my_subwf.compile_into_workflow(ctx, a=5)
            assert len(dynamic_job_spec._nodes) == 5


def test_list_output():
    @task
    def t1(a: int) -> str:
        a = a + 2
        return "world-" + str(a)

    @workflow
    def lister() -> typing.List[str]:
        s = []
        # FYI: For users who happen to look at this, keep in mind this is only run once at compile time.
        for i in range(10):
            s.append(t1(a=i))
        return s

    assert len(lister.interface.outputs) == 1
    binding_data = lister._output_bindings[0].binding  # the property should be named binding_data
    assert binding_data.collection is not None
    assert len(binding_data.collection.bindings) == 10


def test_comparison_refs():
    def dummy_node(id) -> SdkNode:
        n = SdkNode(id, [], None, None, sdk_task=SQLTask("x", "x", [], metadata()))
        n._id = id
        return n

    px = Promise("x", NodeOutput(var="x", sdk_type=LiteralType(simple=SimpleType.INTEGER), sdk_node=dummy_node("n1")))
    py = Promise("y", NodeOutput(var="y", sdk_type=LiteralType(simple=SimpleType.INTEGER), sdk_node=dummy_node("n2")))

    def print_expr(expr):
        print(f"{expr} is type {type(expr)}")

    print_expr(px == py)
    print_expr(px < py)
    print_expr((px == py) & (px < py))
    print_expr(((px == py) & (px < py)) | (px > py))
    print_expr(px < 5)
    print_expr(px >= 5)


def test_comparison_lits():
    px = Promise("x", TypeEngine.to_literal(None, 5, int, None))
    py = Promise("y", TypeEngine.to_literal(None, 8, int, None))

    def eval_expr(expr, expected: bool):
        print(f"{expr} evals to {expr.eval()}")
        assert expected == expr.eval()

    eval_expr(px == py, False)
    eval_expr(px < py, True)
    eval_expr((px == py) & (px < py), False)
    eval_expr(((px == py) & (px < py)) | (px > py), False)
    eval_expr(px < 5, False)
    eval_expr(px >= 5, True)
    eval_expr(py >= 5, True)


def test_wf1_branches():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str) -> str:
        return a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = (
            conditional("test1")
            .if_(x == 4)
            .then(t2(a=b))
            .elif_(x >= 5)
            .then(t2(a=y))
            .else_()
            .fail("Unable to choose branch")
        )
        f = conditional("test2").if_(d == "hello ").then(t2(a="It is hello")).else_().then(t2(a="Not Hello!"))
        return x, f

    x = my_wf(a=5, b="hello ")
    assert x == (7, "Not Hello!")

    x = my_wf(a=2, b="hello ")
    assert x == (4, "It is hello")


def test_wf1_branches_no_else():
    with pytest.raises(NotImplementedError):

        def foo():
            @task
            def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
                return a + 2, "world"

            @task
            def t2(a: str) -> str:
                return a

            @workflow
            def my_wf(a: int, b: str) -> (int, str):
                x, y = t1(a=a)
                d = conditional("test1").if_(x == 4).then(t2(a=b)).elif_(x >= 5).then(t2(a=y))
                conditional("test2").if_(x == 4).then(t2(a=b)).elif_(x >= 5).then(t2(a=y)).else_().fail("blah")
                return x, d

        foo()


def test_wf1_branches_failing():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: str) -> str:
        return a

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = (
            conditional("test1")
            .if_(x == 4)
            .then(t2(a=b))
            .elif_(x >= 5)
            .then(t2(a=y))
            .else_()
            .fail("All Branches failed")
        )
        return x, d

    with pytest.raises(ValueError):
        my_wf(a=1, b="hello ")


def test_cant_use_normal_tuples():
    with pytest.raises(RestrictedTypeError):

        @task
        def t1(a: str) -> tuple:
            return (a, 3)


def test_file_type_in_workflow_with_bad_format():
    @task
    def t1() -> FlyteFile["txt"]:
        fname = "/tmp/flytekit_test"
        with open(fname, "w") as fh:
            fh.write("Hello World\n")
        return fname

    @workflow
    def my_wf() -> FlyteFile["txt"]:
        f = t1()
        return f

    res = my_wf()
    with open(res, "r") as fh:
        assert fh.read() == "Hello World\n"


def test_file_handling_remote_default_wf_input():
    SAMPLE_DATA = "https://raw.githubusercontent.com/jbrownlee/Datasets/master/pima-indians-diabetes.data.csv"

    @task
    def t1(fname: os.PathLike) -> int:
        with open(fname, "r") as fh:
            x = len(fh.readlines())

        return x

    @workflow
    def my_wf(fname: os.PathLike = SAMPLE_DATA) -> int:
        length = t1(fname=fname)
        return length

    assert my_wf._native_interface.inputs_with_defaults["fname"][1] == SAMPLE_DATA
    sample_lp = flytekit.LaunchPlan.create("test_launch_plan", my_wf)
    assert sample_lp.parameters.parameters["fname"].default.scalar.blob.uri == SAMPLE_DATA


def test_file_handling_local_file_gets_copied():
    @task
    def t1() -> FlyteFile:
        # Use this test file itself, since we know it exists.
        return __file__

    @workflow
    def my_wf() -> FlyteFile:
        return t1()

    random_dir = context_manager.FlyteContext.current_context().file_access.get_random_local_directory()
    fs = FileAccessProvider(local_sandbox_dir=random_dir)
    with context_manager.FlyteContext.current_context().new_file_access_context(file_access_provider=fs):
        top_level_files = os.listdir(random_dir)
        assert len(top_level_files) == 1  # the mock_remote folder

        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 0  # the mock_remote folder itself is empty

        x = my_wf()

        # After running, this test file should've been copied to the mock remote location.
        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 1
        # File should've been copied to the mock remote folder
        assert x.path.startswith(random_dir)


def test_file_handling_local_file_gets_force_no_copy():
    @task
    def t1() -> FlyteFile:
        # Use this test file itself, since we know it exists.
        return FlyteFile(__file__, remote_path=False)

    @workflow
    def my_wf() -> FlyteFile:
        return t1()

    random_dir = context_manager.FlyteContext.current_context().file_access.get_random_local_directory()
    fs = FileAccessProvider(local_sandbox_dir=random_dir)
    with context_manager.FlyteContext.current_context().new_file_access_context(file_access_provider=fs):
        top_level_files = os.listdir(random_dir)
        assert len(top_level_files) == 1  # the mock_remote folder

        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 0  # the mock_remote folder itself is empty

        workflow_output = my_wf()

        # After running, this test file should've been copied to the mock remote location.
        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 0

        # Because Flyte doesn't presume to handle a uri that look like a raw path, the path that is returned is
        # the original.
        assert workflow_output.path == __file__


def test_file_handling_remote_file_handling():
    SAMPLE_DATA = "https://raw.githubusercontent.com/jbrownlee/Datasets/master/pima-indians-diabetes.data.csv"

    @task
    def t1() -> FlyteFile:
        return SAMPLE_DATA

    @workflow
    def my_wf() -> FlyteFile:
        return t1()

    random_dir = context_manager.FlyteContext.current_context().file_access.get_random_local_directory()
    print(f"dir: {random_dir}")
    fs = FileAccessProvider(local_sandbox_dir=random_dir)
    with context_manager.FlyteContext.current_context().new_file_access_context(file_access_provider=fs):
        top_level_files = os.listdir(random_dir)
        assert len(top_level_files) == 1  # the mock_remote folder

        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 0  # the mock_remote folder itself is empty

        workflow_output = my_wf()

        # After running the mock remote dir should still be empty, since the workflow_output has not been used
        mock_remote_files = os.listdir(os.path.join(random_dir, "mock_remote"))
        assert len(mock_remote_files) == 0

        # While the literal returned by t1 does contain the web address as the uri, because it's a remote address,
        # flytekit will translate it back into a FlyteFile object on the local drive (but not download it)
        assert workflow_output.path.startswith(random_dir)
        # But the remote source should still be the https address
        assert workflow_output.remote_source == SAMPLE_DATA


def test_wf1_df():
    @task
    def t1(a: int) -> pandas.DataFrame:
        return pandas.DataFrame(data={"col1": [a, 2], "col2": [a, 4]})

    @task
    def t2(df: pandas.DataFrame) -> pandas.DataFrame:
        return df.append(pandas.DataFrame(data={"col1": [5, 10], "col2": [5, 10]}))

    @workflow
    def my_wf(a: int) -> pandas.DataFrame:
        df = t1(a=a)
        return t2(df=df)

    x = my_wf(a=20)
    assert isinstance(x, pandas.DataFrame)
    result_df = x.reset_index(drop=True) == pandas.DataFrame(
        data={"col1": [20, 2, 5, 10], "col2": [20, 4, 5, 10]}
    ).reset_index(drop=True)
    assert result_df.all().all()


def test_lp_default_handling():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @workflow
    def my_wf(a: int, b: int) -> (str, str, int, int):
        x, y = t1(a=a)
        u, v = t1(a=b)
        return y, v, x, u

    lp = launch_plan.LaunchPlan.create("test1", my_wf)
    assert len(lp.parameters.parameters) == 0
    assert len(lp.fixed_inputs.literals) == 0

    lp_with_defaults = launch_plan.LaunchPlan.create("test2", my_wf, default_inputs={"a": 3})
    assert len(lp_with_defaults.parameters.parameters) == 1
    assert len(lp_with_defaults.fixed_inputs.literals) == 0

    lp_with_fixed = launch_plan.LaunchPlan.create("test3", my_wf, fixed_inputs={"a": 3})
    assert len(lp_with_fixed.parameters.parameters) == 0
    assert len(lp_with_fixed.fixed_inputs.literals) == 1

    @workflow
    def my_wf2(a: int, b: int = 42) -> (str, str, int, int):
        x, y = t1(a=a)
        u, v = t1(a=b)
        return y, v, x, u

    lp = launch_plan.LaunchPlan.create("test4", my_wf2)
    assert len(lp.parameters.parameters) == 1
    assert len(lp.fixed_inputs.literals) == 0

    lp_with_defaults = launch_plan.LaunchPlan.create("test5", my_wf2, default_inputs={"a": 3})
    assert len(lp_with_defaults.parameters.parameters) == 2
    assert len(lp_with_defaults.fixed_inputs.literals) == 0
    # Launch plan defaults override wf defaults
    assert lp_with_defaults(b=3) == ("world-5", "world-5", 5, 5)

    lp_with_fixed = launch_plan.LaunchPlan.create("test6", my_wf2, fixed_inputs={"a": 3})
    assert len(lp_with_fixed.parameters.parameters) == 1
    assert len(lp_with_fixed.fixed_inputs.literals) == 1
    # Launch plan defaults override wf defaults
    assert lp_with_fixed(b=3) == ("world-5", "world-5", 5, 5)

    lp_with_fixed = launch_plan.LaunchPlan.create("test7", my_wf2, fixed_inputs={"b": 3})
    assert len(lp_with_fixed.parameters.parameters) == 0
    assert len(lp_with_fixed.fixed_inputs.literals) == 1


def test_wf1_with_lp_node():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @workflow
    def my_subwf(a: int) -> (str, str):
        x, y = t1(a=a)
        u, v = t1(a=x)
        return y, v

    lp = launch_plan.LaunchPlan.create("lp_nodetest1", my_subwf)
    lp_with_defaults = launch_plan.LaunchPlan.create("lp_nodetest2", my_subwf, default_inputs={"a": 3})

    @workflow
    def my_wf(a: int = 42) -> (int, str, str):
        x, y = t1(a=a).with_overrides()
        u, v = lp(a=x)
        return x, u, v

    x = my_wf(a=5)
    assert x == (7, "world-9", "world-11")

    assert my_wf() == (44, "world-46", "world-48")

    @workflow
    def my_wf2(a: int = 42) -> (int, str, str, str):
        x, y = t1(a=a).with_overrides()
        u, v = lp_with_defaults()
        return x, y, u, v

    assert my_wf2() == (44, "world-44", "world-5", "world-7")

    @workflow
    def my_wf3(a: int = 42) -> (int, str, str, str):
        x, y = t1(a=a).with_overrides()
        u, v = lp_with_defaults(a=x)
        return x, y, u, v

    assert my_wf2() == (44, "world-44", "world-5", "world-7")


def test_lp_serialize():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @task
    def t2(a: str, b: str) -> str:
        return b + a

    @workflow
    def my_subwf(a: int) -> (str, str):
        x, y = t1(a=a)
        u, v = t1(a=x)
        return y, v

    lp = launch_plan.LaunchPlan.create("serialize_test1", my_subwf)
    lp_with_defaults = launch_plan.LaunchPlan.create("serialize_test2", my_subwf, default_inputs={"a": 3})

    registration_settings = context_manager.RegistrationSettings(
        project="proj",
        domain="dom",
        version="123",
        image_config=ImageConfig(Image(name="name", fqn="asdf/fdsa", tag="123")),
        env={},
        iam_role="test:iam:role",
        service_account=None,
    )
    with context_manager.FlyteContext.current_context().new_registration_settings(
        registration_settings=registration_settings
    ):
        sdk_lp = lp.get_registerable_entity()
        assert len(sdk_lp.default_inputs.parameters) == 0
        assert len(sdk_lp.fixed_inputs.literals) == 0

        sdk_lp = lp_with_defaults.get_registerable_entity()
        assert len(sdk_lp.default_inputs.parameters) == 1
        assert len(sdk_lp.fixed_inputs.literals) == 0

        # Adding a check to make sure oneof is respected. Tricky with booleans... if a default is specified, the
        # required field needs to be None, not False.
        parameter_a = sdk_lp.default_inputs.parameters["a"]
        parameter_a = Parameter.from_flyte_idl(parameter_a.to_flyte_idl())
        assert parameter_a.default is not None


def test_wf_container_task():
    @task
    def t1(a: int) -> (int, str):
        return a + 2, str(a) + "-HELLO"

    t2 = ContainerTask(
        "raw",
        image="alpine",
        inputs=kwtypes(a=int, b=str),
        input_data_dir="/tmp",
        output_data_dir="/tmp",
        command=["cat"],
        arguments=["/tmp/a"],
        metadata=metadata(),
    )

    def wf(a: int):
        x, y = t1(a=a)
        t2(a=x, b=y)

    with task_mock(t2) as mock:
        mock.side_effect = lambda a, b: None
        assert t2(a=10, b="hello") is None

        wf(a=10)


def test_wf_container_task_multiple():
    square = ContainerTask(
        name="square",
        metadata=metadata(),
        input_data_dir="/var/inputs",
        output_data_dir="/var/outputs",
        inputs=kwtypes(val=int),
        outputs=kwtypes(out=int),
        image="alpine",
        command=["sh", "-c", "echo $(( {{.Inputs.val}} * {{.Inputs.val}} )) | tee /var/outputs/out"],
    )

    sum = ContainerTask(
        name="sum",
        metadata=metadata(),
        input_data_dir="/var/flyte/inputs",
        output_data_dir="/var/flyte/outputs",
        inputs=kwtypes(x=int, y=int),
        outputs=kwtypes(out=int),
        image="alpine",
        command=["sh", "-c", "echo $(( {{.Inputs.x}} + {{.Inputs.y}} )) | tee /var/flyte/outputs/out"],
    )

    @workflow
    def raw_container_wf(val1: int, val2: int) -> int:
        return sum(x=square(val=val1), y=square(val=val2))

    with task_mock(square) as square_mock, task_mock(sum) as sum_mock:
        square_mock.side_effect = lambda val: val * val
        assert square(val=10) == 100

        sum_mock.side_effect = lambda x, y: x + y
        assert sum(x=10, y=10) == 20

        assert raw_container_wf(val1=10, val2=10) == 200


def test_wf_tuple_fails():
    with pytest.raises(RestrictedTypeError):

        @task
        def t1(a: tuple) -> (int, str):
            return a[0] + 2, str(a) + "-HELLO"


def test_wf_typed_schema():
    schema1 = FlyteSchema[kwtypes(x=int, y=str)]

    @task
    def t1() -> schema1:
        s = schema1()
        s.open().write(pandas.DataFrame(data={"x": [1, 2], "y": ["3", "4"]}))
        return s

    @task
    def t2(s: FlyteSchema[kwtypes(x=int, y=str)]) -> FlyteSchema[kwtypes(x=int)]:
        df = s.open().all()
        return df[s.column_names()[:-1]]

    @workflow
    def wf() -> FlyteSchema[kwtypes(x=int)]:
        return t2(s=t1())

    w = t1()
    assert w is not None
    df = w.open(override_mode=SchemaOpenMode.READ).all()
    result_df = df.reset_index(drop=True) == pandas.DataFrame(data={"x": [1, 2], "y": ["3", "4"]}).reset_index(
        drop=True
    )
    assert result_df.all().all()

    df = t2(s=w.as_readonly())
    assert df is not None
    result_df = df.reset_index(drop=True) == pandas.DataFrame(data={"x": [1, 2]}).reset_index(drop=True)
    assert result_df.all().all()

    x = wf()
    df = x.open().all()
    result_df = df.reset_index(drop=True) == pandas.DataFrame(data={"x": [1, 2]}).reset_index(drop=True)
    assert result_df.all().all()


def test_ref():
    @task(
        task_config=TaskReference(
            project="flytesnacks",
            domain="development",
            name="recipes.aaa.simple.join_strings",
            version="553018f39e519bdb2597b652639c30ce16b99c79",
        )
    )
    def ref_t1(a: typing.List[str]) -> str:
        ...

    assert ref_t1.id.project == "flytesnacks"
    assert ref_t1.id.domain == "development"
    assert ref_t1.id.name == "recipes.aaa.simple.join_strings"
    assert ref_t1.id.version == "553018f39e519bdb2597b652639c30ce16b99c79"

    registration_settings = context_manager.RegistrationSettings(
        project="proj",
        domain="dom",
        version="123",
        image_config=ImageConfig(Image(name="name", fqn="asdf/fdsa", tag="123")),
        env={},
        iam_role="test:iam:role",
        service_account=None,
    )
    with context_manager.FlyteContext.current_context().new_registration_settings(
        registration_settings=registration_settings
    ):
        sdk_task = ref_t1.get_registerable_entity()
        assert sdk_task.has_registered
        assert sdk_task.id.project == "flytesnacks"
        assert sdk_task.id.domain == "development"
        assert sdk_task.id.name == "recipes.aaa.simple.join_strings"
        assert sdk_task.id.version == "553018f39e519bdb2597b652639c30ce16b99c79"


def test_ref_task_more():
    @task(
        task_config=TaskReference(
            project="flytesnacks",
            domain="development",
            name="recipes.aaa.simple.join_strings",
            version="553018f39e519bdb2597b652639c30ce16b99c79",
        )
    )
    def ref_t1(a: typing.List[str]) -> str:
        ...

    @workflow
    def wf1(in1: typing.List[str]) -> str:
        return ref_t1(a=in1)

    with pytest.raises(Exception) as e:
        wf1(in1=["hello", "world"])
    assert "Remote reference tasks cannot be run" in f"{e}"

    with task_mock(ref_t1) as mock:
        mock.return_value = "hello"
        assert wf1(in1=["hello", "world"]) == "hello"


def test_dict_wf_with_constants():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        return a + 2, "world"

    @task
    def t2(a: typing.Dict[str, str]) -> str:
        return " ".join([v for k, v in a.items()])

    @workflow
    def my_wf(a: int, b: str) -> (int, str):
        x, y = t1(a=a)
        d = t2(a={"key1": b, "key2": y})
        return x, d

    x = my_wf(a=5, b="hello")
    assert x == (7, "hello world")


def test_dict_wf_with_conversion():
    @task
    def t1(a: int) -> typing.Dict[str, str]:
        return {"a": str(a)}

    @task
    def t2(a: dict) -> str:
        print(f"HAHAH {a}")
        return " ".join([v for k, v in a.items()])

    @workflow
    def my_wf(a: int) -> str:
        return t2(a=t1(a=a))

    with pytest.raises(TypeError):
        my_wf(a=5)


def test_reference_workflow():
    @task
    def t1(a: int) -> typing.NamedTuple("OutputsBC", t1_int_output=int, c=str):
        a = a + 2
        return a, "world-" + str(a)

    @workflow(reference=WorkflowReference(project="proj", domain="developement", name="wf_name", version="abc"))
    def ref_wf1(a: int) -> (str, str):
        ...

    @workflow
    def my_wf(a: int, b: str) -> (int, str, str):
        x, y = t1(a=a).with_overrides()
        u, v = ref_wf1(a=x)
        return x, u, v

    with pytest.raises(Exception):
        my_wf(a=3, b="foo")

    @patch(ref_wf1)
    def inner_test(ref_mock):
        ref_mock.return_value = ("hello", "alice")
        x, y, z = my_wf(a=3, b="foo")
        assert x == 5
        assert y == "hello"
        assert z == "alice"

    inner_test()

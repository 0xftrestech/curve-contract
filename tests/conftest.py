import json
import pytest
import warnings

from brownie.project.main import get_loaded_projects
from pathlib import Path

from brownie._config import CONFIG

# functions in wrapped methods are renamed to simplify common tests

WRAPPED_COIN_METHODS = {
    "cERC20": {
        "get_rate": "exchangeRateStored",
        "mint": "mint",
    },
    "renERC20": {
        "get_rate": "exchangeRateCurrent",
        "mint": "mint",
    },
    "yERC20": {
        "get_rate": "getPricePerFullShare",
        "mint": "deposit",
    },
}

pytest_plugins = [
    "fixtures.accounts",
    "fixtures.deployments",
    "fixtures.functions",
    "fixtures.pooldata",
    "fixtures.setup",
]

_pooldata = {}


def pytest_addoption(parser):
    parser.addoption("--pool", help="comma-separated list of pools to target",)


def pytest_configure(config):
    # add custom markers
    config.addinivalue_line("markers", "target_pool: run test against one or more specific pool")
    config.addinivalue_line("markers", "skip_pool: exclude one or more pools in this test")
    config.addinivalue_line("markers", "lending: only run test against pools that involve lending")
    config.addinivalue_line("markers", "zap: only run test against pools with a deposit contract")
    config.addinivalue_line(
        "markers",
        "itercoins: parametrize a test with one or more ranges, equal to the length "
        "of `wrapped_coins` for the active pool"
    )


def pytest_sessionstart():
    # load `pooldata.json` for each pool
    project = get_loaded_projects()[0]
    for path in [i for i in project._path.glob("contracts/pools/*") if i.is_dir()]:
        with path.joinpath('pooldata.json').open() as fp:
            _pooldata[path.name] = json.load(fp)
            _pooldata[path.name].update(
                name=path.name,
                swap_contract=next(i.stem for i in path.glob(f"StableSwap*"))
            )
            zap_contract = next((i.stem for i in path.glob(f"Deposit*")), None)
            if zap_contract:
                _pooldata[path.name]['zap_contract'] = zap_contract

    # create pooldata for templates
    lp_contract = sorted(i._name for i in project if i._name.startswith("CurveToken"))[-1]

    for path in [i for i in project._path.glob("contracts/pool-templates/*") if i.is_dir()]:
        with path.joinpath('pooldata.json').open() as fp:
            name = f"template-{path.name}"
            _pooldata[name] = json.load(fp)
            _pooldata[name].update(
                name=name,
                lp_contract=lp_contract,
                swap_contract=next(i.stem for i in path.glob(f"*Swap*")),
            )
            zap_contract = next((i.stem for i in path.glob(f"Deposit*")), None)
            if zap_contract:
                _pooldata[name]['zap_contract'] = zap_contract


def pytest_generate_tests(metafunc):
    project = get_loaded_projects()[0]
    itercoins_bound = max(len(i['coins']) for i in _pooldata.values())

    if "pool_data" in metafunc.fixturenames:
        # parametrize `pool_data`
        test_path = Path(metafunc.definition.fspath).relative_to(project._path)
        if test_path.parts[1] in ("pools", "zaps"):
            # parametrize common pool/zap tests to run against all pools
            if test_path.parts[2] == "common":
                if metafunc.config.getoption("pool"):
                    params = metafunc.config.getoption("pool").split(',')
                else:
                    params = list(_pooldata)
                if test_path.parts[1] == "zaps":
                    # for zap tests, filter by pools that have a Deposit contract
                    params = [i for i in params if _pooldata[i].get("zap_contract")]
            else:
                # run targetted pool/zap tests against only the specific pool
                params = [test_path.parts[2]]
        else:
            # pool tests outside `tests/pools` or `tests/zaps` will only run when
            # a target pool is explicitly declared
            try:
                params = metafunc.config.getoption("pool").split(',')
            except Exception:
                params = []
                warnings.warn(
                    f"'{test_path.as_posix()}' contains pool tests, but is outside of "
                    "'tests/pools/'. To run it, specify a pool with `--pool [name]`"
                )
        metafunc.parametrize("pool_data", params, indirect=True, scope="session")

        # apply initial parametrization of `itercoins`
        for marker in metafunc.definition.iter_markers(name="itercoins"):
            for item in marker.args:
                metafunc.parametrize(item, range(itercoins_bound))


def pytest_collection_modifyitems(config, items):
    project = get_loaded_projects()[0]
    is_forked = "fork" in CONFIG.active_network['id']

    for item in items.copy():
        try:
            params = item.callspec.params
            data = _pooldata[params['pool_data']]
        except Exception:
            continue

        # during forked tests, filter pools where pooldata does not contain deployment addresses
        if is_forked and next((i for i in data["coins"] if "underlying_address" not in i), False):
            items.remove(item)
            continue

        # remove excess `itercoins` parametrized tests
        for marker in item.iter_markers(name="itercoins"):
            values = [params[i] for i in marker.args]
            if max(values) >= len(data['coins']) or len(set(values)) < len(values):
                items.remove(item)
                break

        if item not in items:
            continue

        # apply `skip_pool` marker
        for marker in item.iter_markers(name="skip_pool"):
            if params["pool_data"] in marker.args:
                items.remove(item)

        # apply `target_pool` marker
        for marker in item.iter_markers(name="target_pool"):
            if params["pool_data"] not in marker.args:
                items.remove(item)

        # apply `lending` marker
        for marker in item.iter_markers(name="lending"):
            deployer = getattr(project, data['swap_contract'])
            if "exchange_underlying" not in deployer.signatures:
                items.remove(item)

        # apply `lending` marker
        for marker in item.iter_markers(name="zap"):
            if "zap_contract" not in data:
                items.remove(item)

    # hacky magic to ensure the correct number of tests is shown in collection report
    config.pluginmanager.get_plugin("terminalreporter")._numcollected = len(items)


# isolation setup

@pytest.fixture(autouse=True)
def isolation_setup(fn_isolation):
    pass


# main parametrized fixture, used to pass data about each pool into the other fixtures

@pytest.fixture(scope="module")
def pool_data(request):
    project = get_loaded_projects()[0]

    if hasattr(request, "param"):
        pool_name = request.param
    else:
        test_path = Path(request.fspath).relative_to(project._path)
        # ("tests", "pools" or "zaps", pool_name, ...)
        pool_name = test_path.parts[2]
    yield _pooldata[pool_name]


@pytest.fixture(scope="session")
def project():
    yield get_loaded_projects()[0]


@pytest.fixture(scope="session")
def is_forked():
    yield "fork" in CONFIG.active_network['id']

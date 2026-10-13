import pytest

from onboard_sync.config import Settings
from onboard_sync.scenarios import T0, Harness, run
from onboard_sync.service import Service
from onboard_sync.timeutil import Clock


def make_service(**settings) -> Service:
    return Service(clock=Clock(T0), settings=Settings(claude_mode="stub", retry_base_seconds=0, **settings))


@pytest.fixture
def svc() -> Service:
    return make_service()


@pytest.fixture
def h(svc) -> Harness:
    return Harness(svc, verbose=False)


def scenario(sid: str):
    return run(sid, verbose=False)


def okta_users_for(h: Harness, hire_id: str) -> list[dict]:
    return [u for u in h.world.state["okta"]["users"].values() if u["profile"].get("hireId") == hire_id]


def calls(h: Harness, key: str) -> int:
    return h.world.state["calls"].get(key, 0)


def target(h: Harness, hire_id: str, name: str) -> dict:
    t = h.svc.targets[name]
    return t.fetch(h.svc.store.get_hire(hire_id).source_refs[name])

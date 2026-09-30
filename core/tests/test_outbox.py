import pytest

from veldbridge.outbox import Outbox, Prio
from veldbridge.transports import FakeClock, FakeMesh

POCKET = 0xA1B2C3D4
GATEWAY = 0x11111111


@pytest.fixture
def env():
    clock, mesh, done = FakeClock(), FakeMesh(), []
    box = Outbox(mesh, clock, pocket_node_num=POCKET, budget=200, min_interval_s=10,
                 ack_timeout_s=60, retry_schedule_s=(60, 180, 600),
                 on_done=lambda p, ok: done.append((p.text, ok)))
    return clock, mesh, box, done


def test_one_packet_per_10s(env):
    clock, mesh, box, _ = env
    for i in range(3):
        box.enqueue(f"m{i}", Prio.MSG)
    box.tick()
    box.tick()
    assert mesh.texts == ["m0"]
    clock.advance(9.9)
    box.tick()
    assert len(mesh.sent) == 1
    clock.advance(0.1)
    box.tick()
    assert mesh.texts == ["m0", "m1"]


def test_priority_order(env):
    _, mesh, box, _ = env
    box.enqueue("msg", Prio.MSG)
    box.enqueue("probe", Prio.PROBE)
    box.enqueue("sys", Prio.SYS)
    box.tick()
    assert mesh.texts == ["sys"]


def test_sys_replies_coalesce(env):
    _, mesh, box, done = env
    box.enqueue("✓ Sam", Prio.SYS)
    box.enqueue("✓ Jo", Prio.SYS)
    box.tick()
    assert mesh.texts == ["✓ Sam\n✓ Jo"]
    box.on_ack(mesh.last()[0], POCKET, "NONE")
    assert done == [("✓ Sam\n✓ Jo", True)]


def test_ack_only_counts_from_pocket_node(env):
    clock, mesh, box, done = env
    box.enqueue("hello", Prio.MSG)
    box.tick()
    pid = mesh.last()[0]
    box.on_ack(pid, GATEWAY, "NONE")  # implicit ack: ignore
    assert done == []
    clock.advance(4.2)
    box.on_ack(pid, POCKET, "NONE")
    assert done == [("hello", True)]
    assert box.last_rtt == pytest.approx(4.2)


def test_retry_schedule_then_park(env):
    clock, mesh, box, done = env
    box.enqueue("hello", Prio.MSG)
    box.tick()                      # attempt 1 at t=0
    clock.advance(60)
    box.tick()                      # timeout -> wait 60 s
    assert len(mesh.sent) == 1
    clock.advance(60)
    box.tick()                      # attempt 2 at t=120
    assert len(mesh.sent) == 2
    box.on_ack(mesh.last()[0], POCKET, "MAX_RETRANSMIT")  # NAK -> wait 180 s
    clock.advance(179)
    box.tick()
    assert len(mesh.sent) == 2
    clock.advance(1)
    box.tick()                      # attempt 3
    assert len(mesh.sent) == 3
    clock.advance(60)
    box.tick()                      # timeout -> wait 600 s
    clock.advance(600)
    box.tick()                      # attempt 4
    assert len(mesh.sent) == 4
    assert done == []
    clock.advance(60)
    box.tick()                      # schedule exhausted
    assert done == [("hello", False)]
    assert box.active() == []


def test_late_ack_for_earlier_attempt_still_counts(env):
    clock, mesh, box, done = env
    box.enqueue("hello", Prio.MSG)
    box.tick()
    first = mesh.last()[0]
    clock.advance(60)
    box.tick()                      # timed out, now waiting
    box.on_ack(first, POCKET, "NONE")
    assert done == [("hello", True)]


def test_no_retry_packets_park_after_one_failure(env):
    clock, _, box, done = env
    box.enqueue("probe", Prio.PROBE, retries=False)
    box.tick()
    clock.advance(60)
    box.tick()
    assert done == [("probe", False)]


def test_mesh_down_does_not_lose_packet(env):
    clock, mesh, box, _ = env
    mesh.connected = False
    box.enqueue("hello", Prio.MSG)
    box.tick()
    assert mesh.sent == []
    mesh.connected = True
    clock.advance(10)
    box.tick()
    assert mesh.texts == ["hello"]


def test_over_budget_rejected(env):
    _, _, box, _ = env
    with pytest.raises(ValueError):
        box.enqueue("x" * 201, Prio.MSG)

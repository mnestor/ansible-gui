import threading

from ansible_gui.runs.store import RunStore


def test_store_survives_concurrent_reads_and_writes(tmp_path):
    """The executor writes a run while web requests read it; with a shared connection that
    raced, reads returned half-built rows (InterfaceError, or None where JSON was expected)."""
    store = RunStore(tmp_path)
    run = store.create(playbook="p.yml", target="", tags="", skip_tags="", check_mode=True, diff=False,
                       verbosity=0, expected_hosts=["a"], trigger="t")
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        try:
            for i in range(6000):
                store.update(run.id, host_stats={"a": {"ok": i}}, expected_hosts=["a", str(i)])
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            stop.set()

    def reader():
        try:
            while not stop.is_set():
                got = store.get(run.id)
                assert got is not None and isinstance(got.host_stats, dict)
                store.recent(5)
                store.last_status_by_host()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors[:3]

"""The desktop shutdown must retain its notifier through controller cleanup."""

from app import dependencies


def test_controller_can_queue_final_notice_before_notifier_drains():
    calls = []

    class Notifier:
        accepting = True

        def shutdown(self):
            calls.append("notifier_shutdown")
            self.accepting = False

    notifier = Notifier()

    class Controller:
        def shutdown(self):
            calls.append("controller_shutdown")
            assert notifier.accepting, "controller final notice was dropped"
            calls.append("final_notice_accepted")

    class Manager:
        def shutdown(self):
            calls.append("manager_shutdown")

    dependencies.init_globals(Manager(), Controller(), notifier)
    try:
        dependencies.shutdown_globals()
        assert calls == [
            "controller_shutdown", "final_notice_accepted",
            "manager_shutdown", "notifier_shutdown",
        ]
    finally:
        dependencies.init_globals(None, None, None)

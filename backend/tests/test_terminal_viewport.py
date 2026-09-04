import unittest

from backend.app.terminal_viewport import MAX_TERMINAL_DIMENSION, ViewportRegistry


class ViewportRegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = ViewportRegistry()

    def test_single_connection_gets_its_own_size(self):
        self.assertEqual(self.reg.set(1, "a", 40, 120), (40, 120))

    def test_narrow_client_does_not_shrink_the_wide_one(self):
        self.reg.set(1, "desktop", 40, 200)
        # 手机端连上来并上报自己的窄尺寸，PTY 仍按最大值走
        self.assertEqual(self.reg.set(1, "phone", 20, 45), (40, 200))

    def test_dimensions_are_maximised_independently(self):
        self.reg.set(1, "wide", 20, 200)
        self.assertEqual(self.reg.set(1, "tall", 60, 80), (60, 200))

    def test_dropping_the_wide_client_shrinks_back(self):
        self.reg.set(1, "desktop", 40, 200)
        self.reg.set(1, "phone", 20, 45)
        self.assertEqual(self.reg.drop(1, "desktop"), (20, 45))

    def test_dropping_the_last_client_returns_none(self):
        self.reg.set(1, "only", 40, 120)
        self.assertIsNone(self.reg.drop(1, "only"))
        self.assertIsNone(self.reg.effective(1))

    def test_dropping_an_unknown_connection_is_a_noop(self):
        self.reg.set(1, "a", 40, 120)
        self.assertEqual(self.reg.drop(1, "ghost"), (40, 120))

    def test_tasks_are_isolated(self):
        self.reg.set(1, "a", 40, 200)
        self.assertEqual(self.reg.set(2, "b", 20, 45), (20, 45))

    def test_resizing_the_same_connection_replaces_its_entry(self):
        self.reg.set(1, "a", 40, 200)
        self.assertEqual(self.reg.set(1, "a", 30, 100), (30, 100))

    def test_effective_reads_without_mutating(self):
        self.reg.set(1, "a", 40, 200)
        self.assertEqual(self.reg.effective(1), (40, 200))
        self.assertEqual(self.reg.effective(1), (40, 200))

    def test_rejects_non_positive_dimensions(self):
        for rows, cols in ((0, 80), (24, 0), (-1, 80), (24, -1)):
            with self.assertRaises(ValueError):
                self.reg.set(1, "a", rows, cols)

    def test_rejects_absurd_dimensions(self):
        with self.assertRaises(ValueError):
            self.reg.set(1, "a", 24, MAX_TERMINAL_DIMENSION + 1)

    def test_rejected_size_leaves_previous_state_intact(self):
        self.reg.set(1, "a", 40, 200)
        with self.assertRaises(ValueError):
            self.reg.set(1, "a", 0, 0)
        self.assertEqual(self.reg.effective(1), (40, 200))


if __name__ == "__main__":
    unittest.main()

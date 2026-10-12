import pytest

from tests.live import jev

pytestmark = pytest.mark.live


def test_mentioned_value_scores_higher_than_unmentioned():
    value = "satin-7421"
    yes = jev.mentions_value(f"イメージの識別子は {value} です。", value)
    no = jev.mentions_value("今日はよい天気ですね。", value)
    print(f"yes={yes:.3f} no={no:.3f} usage={jev.usage_log}")
    assert yes > no
    assert jev.passes(yes)

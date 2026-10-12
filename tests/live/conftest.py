import pytest

from satin_agent.config import load_dotenv


@pytest.fixture(scope="session", autouse=True)
def _dotenv():
    load_dotenv(".env")

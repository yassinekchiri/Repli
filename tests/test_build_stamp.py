"""The revision stamped into the two generated single-file scripts.

Whoever receives one of those files has nothing else — no checkout, no git —
so the stamp is their only way to say which source it carries. '-dirty' is
therefore a real warning: it means the payload holds edits nobody can
reproduce from the repository. It has to be raised for genuine drift and
never for the build's own output.
"""

import subprocess

import pytest

from tools import payload


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture()
def repo(tmp_path):
    """A tiny repository with one commit."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "source.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "first")
    return tmp_path


def test_a_clean_tree_is_stamped_with_the_bare_commit(repo):
    assert not payload.revision(str(repo)).endswith("-dirty")


def test_an_uncommitted_edit_raises_dirty(repo):
    (repo / "source.py").write_text("x = 2\n")

    assert payload.revision(str(repo)).endswith("-dirty")


def test_an_untracked_file_raises_dirty(repo):
    """It is in the payload but not in the repository: the worst kind of drift."""
    (repo / "extra.py").write_text("y = 1\n")

    assert payload.revision(str(repo)).endswith("-dirty")


def test_rebuilding_the_generated_scripts_does_not_raise_dirty(repo):
    """A build rewrites them by definition. Counting that would mean no
    artifact could ever carry a clean revision: the first build dirties the
    tree the second build inspects, and even a committed one reads '-dirty'."""
    for name in payload.GENERATED:
        (repo / name).write_text("# rebuilt\n")

    assert not payload.revision(str(repo)).endswith("-dirty")


def test_a_generated_script_does_not_mask_a_real_edit(repo):
    (repo / payload.GENERATED[0]).write_text("# rebuilt\n")
    (repo / "source.py").write_text("x = 3\n")

    assert payload.revision(str(repo)).endswith("-dirty")


def test_outside_a_repository_the_revision_is_unknown(tmp_path):
    assert payload.revision(str(tmp_path / "nowhere")) == "unknown"

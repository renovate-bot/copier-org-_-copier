import os
from pathlib import Path

import pytest
from plumbum import local

from copier import run_copy, run_update
from copier.errors import DirtyLocalWarning

from .helpers import build_file_tree, git


def test_copy_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            """,
            repo / "target.txt": "Symlink target",
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert (dst / "symlink.txt").exists()
    assert (dst / "symlink.txt").is_symlink()
    assert (dst / "symlink.txt").readlink() == Path("target.txt")


def test_copy_symlink_templated_name(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            symlink_name: symlink
            """,
            repo / "target.txt": "Symlink target",
            repo / "{{ symlink_name }}.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert (dst / "symlink.txt").exists()
    assert (dst / "symlink.txt").is_symlink()
    assert (dst / "symlink.txt").readlink() == Path("target.txt")


def test_copy_symlink_templated_target(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            target_name: target
            """,
            repo / "{{ target_name }}.txt": "Symlink target",
            repo / "symlink1.txt.jinja": Path("{{ target_name }}.txt"),
            repo / "symlink2.txt": Path("{{ target_name }}.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert (dst / "symlink1.txt").exists()
    assert (dst / "symlink1.txt").is_symlink()
    assert (dst / "symlink1.txt").readlink() == Path("target.txt")

    assert not (dst / "symlink2.txt").exists()
    assert (dst / "symlink2.txt").is_symlink()
    assert (dst / "symlink2.txt").readlink() == Path("{{ target_name }}.txt")


def test_copy_symlink_missing_target(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            """,
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "symlink.txt").is_symlink()
    assert (dst / "symlink.txt").readlink() == Path("target.txt")
    assert not (
        dst / "symlink.txt"
    ).exists()  # exists follows symlinks, It returns False as the target doesn't exist


def test_option_preserve_symlinks_false(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: false
            """,
            repo / "target.txt": "Symlink target",
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert (dst / "symlink.txt").exists()
    assert not (dst / "symlink.txt").is_symlink()


def test_option_preserve_symlinks_default(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            """,
            repo / "target.txt": "Symlink target",
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert (dst / "symlink.txt").exists()
    assert not (dst / "symlink.txt").is_symlink()


def test_update_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))

    build_file_tree(
        {
            src / ".copier-answers.yml.jinja": """\
                # Changes here will be overwritten by Copier
                {{ _copier_answers|to_nice_yaml }}
            """,
            src / "copier.yml": """\
                _preserve_symlinks: true
            """,
            src / "aaaa.txt": """
                Lorem ipsum
            """,
            src / "bbbb.txt": """
                dolor sit amet
            """,
            src / "symlink.txt": Path("./aaaa.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", "-A")
        git("commit", "-m", "first commit on src")

    run_copy(str(src), dst, defaults=True, overwrite=True)

    with local.cwd(src):
        # test updating a symlink
        Path("symlink.txt").unlink()
        os.symlink("bbbb.txt", "symlink.txt")

    # dst must be vcs-tracked to use run_update
    with local.cwd(dst):
        git("init")
        git("add", "-A")
        git("commit", "-m", "first commit on dst")

    # make sure changes have not yet propagated
    p1 = src / "symlink.txt"
    p2 = dst / "symlink.txt"
    assert p1.read_text() != p2.read_text()

    with pytest.warns(DirtyLocalWarning):
        run_update(dst, defaults=True, overwrite=True)

    # make sure changes propagate after update
    p1 = src / "symlink.txt"
    p2 = dst / "symlink.txt"
    assert p1.read_text() == p2.read_text()

    assert (dst / "symlink.txt").readlink() == Path("bbbb.txt")


def test_update_file_to_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))

    build_file_tree(
        {
            src / ".copier-answers.yml.jinja": """\
                # Changes here will be overwritten by Copier
                {{ _copier_answers|to_nice_yaml }}
            """,
            src / "copier.yml": """\
                _preserve_symlinks: true
            """,
            src / "aaaa.txt": """
                Lorem ipsum
            """,
            src / "bbbb.txt": """
                dolor sit amet
            """,
            src / "cccc.txt": Path("./aaaa.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", "-A")
        git("commit", "-m", "first commit on src")

    run_copy(str(src), dst, defaults=True, overwrite=True)

    with local.cwd(src):
        # test updating a symlink
        Path("bbbb.txt").unlink()
        os.symlink("aaaa.txt", "bbbb.txt")
        Path("cccc.txt").unlink()
        with Path("cccc.txt").open("w+") as f:
            f.write("Lorem ipsum")

    # dst must be vcs-tracked to use run_update
    with local.cwd(dst):
        git("init")
        git("add", "-A")
        git("commit", "-m", "first commit on dst")

    # make sure changes have not yet propagated
    assert not (dst / "bbbb.txt").is_symlink()
    assert (dst / "cccc.txt").is_symlink()

    with pytest.warns(DirtyLocalWarning):
        run_update(dst, defaults=True, overwrite=True)

    # make sure changes propagate after update
    assert (dst / "bbbb.txt").is_symlink()
    assert (dst / "bbbb.txt").readlink() == Path("aaaa.txt")
    assert not (dst / "cccc.txt").is_symlink()
    assert (dst / "cccc.txt").read_text() == "Lorem ipsum"


def test_exclude_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            """,
            repo / "target.txt": "Symlink target",
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        exclude=["symlink.txt"],
        vcs_ref="HEAD",
    )
    assert not (dst / "symlink.txt").exists()
    assert not (dst / "symlink.txt").is_symlink()


def test_pretend_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            """,
            repo / "target.txt": "Symlink target",
            repo / "symlink.txt": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        pretend=True,
        vcs_ref="HEAD",
    )
    assert not (dst / "symlink.txt").exists()
    assert not (dst / "symlink.txt").is_symlink()


def test_copy_symlink_none_path(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    # Prepare repo bundle
    repo = src / "repo"
    repo.mkdir()
    build_file_tree(
        {
            repo / "copier.yaml": """\
            _preserve_symlinks: true
            render: false
            """,
            repo / "target.txt": "Symlink target",
            repo / "{% if render %}symlink.txt{% endif %}": Path("target.txt"),
        }
    )

    with local.cwd(src):
        git("init")
        git("add", ".")
        git("commit", "-m", "hello world")

    run_copy(
        str(repo),
        dst,
        defaults=True,
        overwrite=True,
        vcs_ref="HEAD",
    )

    assert (dst / "target.txt").exists()
    assert not (dst / "symlink.txt").exists()
    assert not (dst / "symlink.txt").is_symlink()


def test_recursive_symlink(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "copier.yaml": "_preserve_symlinks: true",
            src / "one" / "two" / "three" / "root": Path("../../../"),
        }
    )
    run_copy(str(src), dst, defaults=True, overwrite=True)
    assert (dst / "one" / "two" / "three" / "root").is_symlink()
    assert (dst / "one" / "two" / "three" / "root").readlink() == Path("../../../")


def test_symlinked_dir_expanded(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "a_dir" / "a_file.txt": "some content",
            src / "a_symlinked_dir": Path("a_dir"),
            src / "a_nested" / "symlink": Path("../a_dir"),
        }
    )
    run_copy(str(src), dst, defaults=True, overwrite=True)
    assert (dst / "a_dir" / "a_file.txt").read_text() == "some content"
    assert (dst / "a_symlinked_dir" / "a_file.txt").read_text() == "some content"
    assert (dst / "a_nested" / "symlink" / "a_file.txt").read_text() == "some content"

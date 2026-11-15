"""Unit-тесты host-only генератора истории выпусков."""
import importlib.util
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("updates_generator", ROOT / "make_updates.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def git(root: Path, *args: str):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding="utf-8")


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig", newline="\n")


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Тест")
    write(root / "one.txt", "первый\n")
    git(root, "add", "one.txt")
    git(root, "commit", "-m", "feat: первый выпуск")
    git(root, "tag", "v1.0.0")
    write(root / "one.txt", "первый\nвторой\n")
    write(root / "binary.bin", "не бинарный, но отдельный файл\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "feat: новая возможность")
    git(root, "tag", "v1.1.0")
    write(root / "one.txt", "первый\nвторой\nразработка\n")
    git(root, "add", "one.txt")
    git(root, "commit", "-m", "fix: продолжаю разработку")
    return root


def test_semver_and_subject_guards():
    assert generator.semver("v2.10.3") == (2, 10, 3)
    for value in ("2.3.0", "v2.3", "v2.3.0-beta", "v02.3.0"):
        with pytest.raises(generator.BuildError):
            generator.semver(value)
    assert generator.safe_subject("feat: обычное изменение") == "feat: обычное изменение"
    assert generator.safe_subject("fix(bot): показываю код группы на кнопках")
    for value in ("", "пароль=abcdefghijklmnop", "код ABC12345", "MAX 1234567890",
                  "https://user:pass@example.test/path", "bad\x00value"):
        with pytest.raises(generator.BuildError):
            generator.safe_subject(value)


def test_parse_numstat_handles_binary_and_rename():
    text = "\x1eabc1234\x1f" + "a" * 40 + "\x1f2026-11-15\x1ffeat: test\n-\t-\timage.png\n4\t2\told => new\n"
    rows = generator.parse_records(text)
    assert rows == ({"hash": "abc1234", "full": "a" * 40, "date": "2026-11-15",
                     "when": "15.11.2026", "subject": "feat: test", "files": 2,
                     "add": 4, "del": 2},)


def test_build_explicit_ranges_source_head_and_atomic_output(tmp_path):
    root = repo(tmp_path)
    output = root / "web" / "updates_data.py"
    snapshot = generator.build(root=root, output=output)
    assert snapshot["source_head"] == git(root, "rev-parse", "--short=7", "HEAD").stdout.strip()
    releases = snapshot["releases"]
    assert [item["version"] for item in releases] == ["0.0.0", "1.1.0", "1.0.0"]
    assert releases[0]["state"] == "development" and releases[0]["tag"] == ""
    assert releases[1]["tag"] == "v1.1.0" and len(releases[1]["commits"]) == 1
    source = output.read_text(encoding="utf-8-sig")
    compile(source, str(output), "exec")
    assert "SOURCE_HEAD" in source and "full" not in source and "binary.bin" not in source
    assert not list(output.parent.glob("updates_data.py.*.tmp"))


def test_build_rejects_dirty_tracked_tree_without_touching_output(tmp_path):
    root = repo(tmp_path)
    output = root / "web" / "updates_data.py"
    write(output, "старое значение\n")
    git(root, "add", "web/updates_data.py")
    git(root, "commit", "-m", "chore: старый снимок")
    output.write_text("незакоммиченная правка\n", encoding="utf-8-sig")
    before = output.read_bytes()
    with pytest.raises(generator.BuildError, match="незакоммиченные"):
        generator.build(root=root, output=output)
    assert output.read_bytes() == before


def test_build_rejects_bad_tag_and_secret_subject(tmp_path):
    root = repo(tmp_path)
    git(root, "tag", "v1.2.0-beta")
    with pytest.raises(generator.BuildError, match="метка"):
        generator.build(root=root, output=root / "updates_data.py")
    git(root, "tag", "-d", "v1.2.0-beta")
    write(root / "secret.txt", "x")
    git(root, "add", "secret.txt")
    git(root, "commit", "-m", "fix: token abcdefghijklmnop")
    with pytest.raises(generator.BuildError, match="ручной проверки"):
        generator.build(root=root, output=root / "updates_data.py")


def test_generator_is_host_only_and_portable():
    source = (ROOT / "make_updates.py").read_text(encoding="utf-8-sig")
    assert "Path(__file__).resolve().parent" in source
    assert "C:\\BOT-LPC" not in source
    assert "os.replace" not in source  # Path.replace используется после полной проверки.
    assert "NamedTemporaryFile" in source and ".replace(output)" in source
    assert "assert " not in source
    assert "fetch" not in source and "github" not in source.lower()

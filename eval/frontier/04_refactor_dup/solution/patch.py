import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1] / "repo"
SOLUTION = Path(__file__).resolve().parent


def apply():
    # Sync solution files into repo: copy/update files present in solution,
    # remove repo files that are not present in solution (except __pycache__).
    sol_files = {p.name for p in SOLUTION.glob("*.py") if p.name != "patch.py"}
    for src in SOLUTION.glob("*.py"):
        if src.name == "patch.py":
            continue
        dst = REPO / src.name
        shutil.copy2(src, dst)
    for dst in REPO.glob("*.py"):
        if dst.name not in sol_files:
            dst.unlink()


if __name__ == "__main__":
    apply()

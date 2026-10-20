"""기존 작업 데이터는 보존하면서 설명 정책 버전 컬럼을 명시적으로 추가한다."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.job_migrations import main


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit E5 preparation. Runtime downloads remain disabled."""
import argparse
from pathlib import Path

MODEL = "intfloat/multilingual-e5-base"
REVISION = "d128750597153bb5987e10b1c3493a34e5a4502a"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    target = Path(args.output)
    from sentence_transformers import SentenceTransformer
    import numpy as np
    if target.exists() and any(target.iterdir()):
        model = SentenceTransformer(str(target), device="cpu", local_files_only=True)
    else:
        model = SentenceTransformer(MODEL, revision=REVISION, device="cpu", trust_remote_code=False)
        target.mkdir(parents=True, exist_ok=True)
        model.save(str(target))
        (target / "UPSTREAM.txt").write_text(MODEL + "\n" + REVISION + "\nMIT; https://huggingface.co/intfloat/multilingual-e5-base\n", encoding="utf-8")
    values = model.encode(["query: community model validation"], normalize_embeddings=True)
    if values.shape != (1, 768) or not np.isfinite(values).all() or not np.isclose(np.linalg.norm(values[0]), 1, atol=1e-4):
        raise SystemExit("E5_VALIDATION_FAILED")
    print("E5_CPU_VALIDATED dimension=768 revision=" + REVISION)

if __name__ == "__main__":
    main()

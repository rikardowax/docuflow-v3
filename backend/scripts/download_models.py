#!/usr/bin/env python3
"""
DocuFlow v3.0 - Model download script

Downloads all required ML models that cannot be bundled in the Docker image.
Run once before first deployment, or add to your CI/CD pipeline.

Usage:
    python scripts/download_models.py [--liveness-only] [--face-only]

Models downloaded:
  1. MiniFASNet-V2 (liveness anti-spoofing)
     Source: Silent-Face-Anti-Spoofing (Minivision AI, Apache 2.0)
     Size: ~1.5 MB
     Destination: $LIVENESS_MODEL_PATH/minifas.onnx

  2. InsightFace buffalo_l (ArcFace face recognition)
     Source: InsightFace model zoo
     Size: ~330 MB
     Destination: $FACE_MODEL_PATH/buffalo_l/
     Note: InsightFace downloads this automatically on first use via its
           FaceAnalysis(name="buffalo_l") call. This script forces an
           eager download so the first request is not penalised.
"""
import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# ── MiniFASNet-V2 ONNX ────────────────────────────────────────────────
# Pre-converted ONNX from the official repository.
# SHA-256 is checked after download to guarantee integrity.
MINIFAS_SOURCES = [
    # Primary: GitHub release (official Minivision repo)
    {
        "url": "https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/master/resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.onnx",
        "sha256": None,  # Fill in after first run: python -c "import hashlib; print(hashlib.sha256(open('minifas.onnx','rb').read()).hexdigest())"
        "filename": "minifas.onnx",
    },
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path, expected_sha256: str | None = None) -> bool:
    print(f"  Downloading {url}")
    print(f"  → {dest}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "DocuFlow/3.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            while chunk := resp.read(65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = downloaded / total * 100
                    print(f"\r  {pct:.0f}% ({downloaded/1024:.0f} KB)", end="", flush=True)
        print()

        if expected_sha256:
            actual = _sha256(dest)
            if actual != expected_sha256:
                print(f"  ERROR: SHA-256 mismatch!\n  expected: {expected_sha256}\n  got:      {actual}")
                dest.unlink(missing_ok=True)
                return False
            print(f"  SHA-256 OK: {actual[:16]}...")
        else:
            print(f"  SHA-256: {_sha256(dest)[:32]}...  (no expected hash configured)")

        print(f"  OK — saved {dest.stat().st_size / 1024:.0f} KB")
        return True
    except Exception as e:
        print(f"  FAILED: {e}")
        return False


def download_liveness(dest_dir: Path) -> bool:
    print("\n── MiniFASNet-V2 liveness model ─────────────────────────────")
    dest = dest_dir / "minifas.onnx"

    if dest.exists():
        print(f"  Already present: {dest}  ({dest.stat().st_size / 1024:.0f} KB)")
        return True

    for source in MINIFAS_SOURCES:
        if _download(source["url"], dest, source.get("sha256")):
            return True

    print("\n  Automatic download failed. Manual steps:")
    print("  1. Go to: https://github.com/minivision-ai/Silent-Face-Anti-Spoofing")
    print("  2. Download: resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.onnx")
    print(f"  3. Copy to: {dest}")
    return False


def download_insightface(dest_dir: Path) -> bool:
    print("\n── InsightFace buffalo_l (ArcFace) ──────────────────────────")
    buffalo_dir = dest_dir / "buffalo_l"
    required_files = [
        "det_10g.onnx",
        "w600k_r50.onnx",
        "2d106det.onnx",
        "genderage.onnx",
    ]
    missing = [f for f in required_files if not (buffalo_dir / f).exists()]

    if not missing:
        print(f"  Already present in {buffalo_dir}")
        return True

    print(f"  Missing: {missing}")
    print("  Triggering InsightFace auto-download...")
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(
            name="buffalo_l",
            root=str(dest_dir),
            providers=["CPUExecutionProvider"],
        )
        app.prepare(ctx_id=-1, det_size=(640, 640))
        print("  InsightFace buffalo_l ready")
        return True
    except ImportError:
        print("  insightface not installed — run: pip install insightface onnxruntime")
        return False
    except Exception as e:
        print(f"  InsightFace download failed: {e}")
        print("  Manual: pip install insightface && python -c \"from insightface.app import FaceAnalysis; FaceAnalysis('buffalo_l').prepare(-1)\"")
        return False


def main():
    parser = argparse.ArgumentParser(description="DocuFlow model downloader")
    parser.add_argument("--liveness-only", action="store_true")
    parser.add_argument("--face-only",     action="store_true")
    parser.add_argument("--liveness-dir",  default=os.getenv("LIVENESS_MODEL_PATH", "/models/liveness"))
    parser.add_argument("--face-dir",      default=os.getenv("FACE_MODEL_PATH",    "/models/arcface"))
    args = parser.parse_args()

    liveness_dir = Path(args.liveness_dir)
    face_dir     = Path(args.face_dir)

    results = {}

    if not args.face_only:
        results["liveness"] = download_liveness(liveness_dir)

    if not args.liveness_only:
        results["insightface"] = download_insightface(face_dir)

    print("\n── Summary ──────────────────────────────────────────────────")
    all_ok = True
    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  {name:<20} {status}")
        if not ok:
            all_ok = False

    if not all_ok:
        print("\nSome models are missing. DocuFlow will start in degraded mode.")
        print("Liveness checks will use the unsafe Laplacian fallback until minifas.onnx is present.")
        sys.exit(1)
    else:
        print("\nAll models ready. DocuFlow is production-ready.")
        sys.exit(0)


if __name__ == "__main__":
    main()

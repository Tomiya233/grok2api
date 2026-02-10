from pathlib import Path


def test_images_endpoint_uses_safe_filename_resolution():
    images_file = Path(__file__).resolve().parents[1] / "app" / "api" / "v1" / "images.py"
    source = images_file.read_text(encoding="utf-8")

    assert "Path(normalized).name" in source
    assert "img_path.replace('-', '/')" not in source

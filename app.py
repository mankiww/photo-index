#!/usr/bin/env python3
"""photo-index web GUI"""

import io
import json
import os
import sqlite3
import struct
from pathlib import Path

from flask import Flask, request, jsonify, send_file, abort

app = Flask(__name__, static_folder="static")
DB_PATH = Path(os.environ.get("PHOTO_INDEX_DB", Path.home() / ".photo-indexer.db"))
THUMB_DIR = Path(os.environ.get("PHOTO_INDEX_THUMBS", Path.home() / ".photo-indexer-thumbs"))
FACE_THUMB_DIR = Path(os.environ.get("PHOTO_INDEX_FACE_THUMBS", Path.home() / ".photo-indexer-face-thumbs"))


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


@app.route("/")
def index():
    return send_file("static/index.html")


# ── Stats ────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as c FROM photos").fetchone()["c"]
    tagged = db.execute(
        "SELECT COUNT(*) as c FROM photos WHERE yolo_tags != '[]' AND yolo_tags != ''"
    ).fetchone()["c"]

    # 상위 태그 (YOLO + custom 합산)
    rows = db.execute("SELECT yolo_tags, custom_tags FROM photos").fetchall()
    tag_counts = {}
    for r in rows:
        for t in json.loads(r["yolo_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
        for t in json.loads(r["custom_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:20]

    sources = db.execute(
        "SELECT source_name, COUNT(*) as c FROM photos GROUP BY source_name ORDER BY c DESC"
    ).fetchall()

    cameras = db.execute(
        "SELECT camera, COUNT(*) as c FROM photos WHERE camera != '' GROUP BY camera ORDER BY c DESC"
    ).fetchall()

    date_range = db.execute(
        "SELECT MIN(date_taken) as mn, MAX(date_taken) as mx FROM photos WHERE date_taken != ''"
    ).fetchone()

    db.close()
    return jsonify({
        "total": total,
        "tagged": tagged,
        "untagged": total - tagged,
        "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        "sources": [{"name": r["source_name"] or "(직접경로)", "count": r["c"]} for r in sources],
        "cameras": [{"name": r["camera"], "count": r["c"]} for r in cameras],
        "date_min": date_range["mn"] or "",
        "date_max": date_range["mx"] or "",
    })


# ── Filters API ──────────────────────────────────────

@app.route("/api/filters")
def api_filters():
    db = get_db()

    # Tags (YOLO + custom 합산)
    rows = db.execute("SELECT yolo_tags, custom_tags FROM photos").fetchall()
    tag_counts = {}
    for r in rows:
        for t in json.loads(r["yolo_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
        for t in json.loads(r["custom_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    tags = sorted(tag_counts.items(), key=lambda x: -x[1])

    # Cameras
    cameras = db.execute(
        "SELECT camera as value, COUNT(*) as count FROM photos WHERE camera != '' GROUP BY camera ORDER BY count DESC"
    ).fetchall()

    # Lenses (+ 렌즈 없음)
    lenses = db.execute(
        "SELECT lens as value, COUNT(*) as count FROM photos WHERE lens != '' GROUP BY lens ORDER BY count DESC"
    ).fetchall()
    no_lens_count = db.execute("SELECT COUNT(*) as c FROM photos WHERE lens = '' OR lens IS NULL").fetchone()["c"]

    # Sources
    sources = db.execute(
        "SELECT source_name as value, COUNT(*) as count FROM photos WHERE source_name != '' GROUP BY source_name ORDER BY count DESC"
    ).fetchall()

    # Dates (YYYY-MM)
    date_rows = db.execute(
        "SELECT DISTINCT substr(replace(date_taken, ':', '-'), 1, 7) as ym FROM photos WHERE date_taken != '' ORDER BY ym DESC"
    ).fetchall()

    db.close()
    return jsonify({
        "tags": [{"value": t, "count": c} for t, c in tags],
        "cameras": [{"value": r["value"], "count": r["count"]} for r in cameras],
        "lenses": [{"value": r["value"], "count": r["count"]} for r in lenses]
                 + ([{"value": "(렌즈 없음)", "count": no_lens_count}] if no_lens_count else []),
        "sources": [{"value": r["value"], "count": r["count"]} for r in sources],
        "dates": [r["ym"] for r in date_rows if r["ym"]],
    })


# ── Search ───────────────────────────────────────────

@app.route("/api/search")
def api_search():
    db = get_db()
    conditions = []
    params = []

    tag = request.args.get("tag", "").strip()
    camera = request.args.get("camera", "").strip()
    lens = request.args.get("lens", "").strip()
    date = request.args.get("date", "").strip()
    source = request.args.get("source", "").strip()
    gps = request.args.get("gps", "").strip()
    face_cluster = request.args.get("face_cluster", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(200, max(1, int(request.args.get("per_page", 60))))

    if tag:
        conditions.append("(yolo_tags LIKE ? OR custom_tags LIKE ?)")
        params.extend([f'%"{tag}"%', f'%"{tag}"%'])
    if camera:
        conditions.append("camera LIKE ?")
        params.append(f"%{camera}%")
    if lens:
        if lens == "(렌즈 없음)":
            conditions.append("(lens = '' OR lens IS NULL)")
        else:
            conditions.append("lens LIKE ?")
            params.append(f"%{lens}%")
    if date:
        conditions.append("date_taken LIKE ?")
        params.append(f"{date.replace('-', ':')}%")
    if source:
        conditions.append("source_name = ?")
        params.append(source)
    if gps == "1":
        conditions.append("gps_lat IS NOT NULL AND gps_lon IS NOT NULL")
    if face_cluster:
        conditions.append("id IN (SELECT photo_id FROM faces WHERE cluster_id = ?)")
        params.append(int(face_cluster))

    where = " AND ".join(conditions) if conditions else "1=1"
    count = db.execute(f"SELECT COUNT(*) as c FROM photos WHERE {where}", params).fetchone()["c"]

    offset = (page - 1) * per_page
    rows = db.execute(
        f"SELECT * FROM photos WHERE {where} ORDER BY date_taken DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    ).fetchall()

    results = []
    for r in rows:
        yolo_tags = json.loads(r["yolo_tags"]) if r["yolo_tags"] else []
        custom_tags = json.loads(r["custom_tags"] or "[]")
        results.append({
            "id": r["id"],
            "filepath": r["filepath"],
            "filename": Path(r["filepath"]).name,
            "source_name": r["source_name"] or "",
            "date_taken": r["date_taken"] or "",
            "camera": r["camera"] or "",
            "lens": r["lens"] or "",
            "iso": r["iso"],
            "gps_lat": r["gps_lat"],
            "gps_lon": r["gps_lon"],
            "tags": yolo_tags,
            "custom_tags": custom_tags,
        })

    db.close()
    return jsonify({
        "total": count,
        "page": page,
        "per_page": per_page,
        "pages": (count + per_page - 1) // per_page,
        "results": results,
    })


# ── Tags CRUD ────────────────────────────────────────

@app.route("/api/photos/<int:photo_id>/tags", methods=["POST"])
def add_tag(photo_id):
    db = get_db()
    row = db.execute("SELECT custom_tags FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        db.close()
        abort(404)

    data = request.get_json()
    tag = (data.get("tag") or "").strip()
    if not tag:
        db.close()
        return jsonify({"error": "tag is required"}), 400

    tags = json.loads(row["custom_tags"] or "[]")
    if tag not in tags:
        tags.append(tag)
        db.execute("UPDATE photos SET custom_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), photo_id))
        db.commit()

    db.close()
    return jsonify({"custom_tags": tags})


@app.route("/api/photos/<int:photo_id>/tags", methods=["DELETE"])
def remove_tag(photo_id):
    db = get_db()
    row = db.execute("SELECT custom_tags FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        db.close()
        abort(404)

    data = request.get_json()
    tag = (data.get("tag") or "").strip()
    if not tag:
        db.close()
        return jsonify({"error": "tag is required"}), 400

    tags = json.loads(row["custom_tags"] or "[]")
    if tag in tags:
        tags.remove(tag)
        db.execute("UPDATE photos SET custom_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), photo_id))
        db.commit()

    db.close()
    return jsonify({"custom_tags": tags})


@app.route("/api/photos/<int:photo_id>/yolo-tags", methods=["DELETE"])
def remove_yolo_tag(photo_id):
    db = get_db()
    row = db.execute("SELECT yolo_tags FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        db.close()
        abort(404)

    data = request.get_json()
    tag = (data.get("tag") or "").strip()
    if not tag:
        db.close()
        return jsonify({"error": "tag is required"}), 400

    tags = json.loads(row["yolo_tags"] or "[]")
    if tag in tags:
        tags.remove(tag)
        db.execute("UPDATE photos SET yolo_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), photo_id))
        db.commit()

    db.close()
    return jsonify({"tags": tags})


# ── Tags list ────────────────────────────────────────

@app.route("/api/tags")
def api_tags():
    db = get_db()
    rows = db.execute("SELECT yolo_tags, custom_tags FROM photos").fetchall()
    tag_counts = {}
    for r in rows:
        for t in json.loads(r["yolo_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
        for t in json.loads(r["custom_tags"] or "[]"):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    db.close()
    return jsonify(sorted(tag_counts.items(), key=lambda x: -x[1]))


# ── Faces API ────────────────────────────────────────

@app.route("/api/faces")
def api_faces():
    """클러스터 목록"""
    db = get_db()
    clusters = db.execute(
        "SELECT * FROM face_clusters ORDER BY face_count DESC"
    ).fetchall()

    result = []
    for c in clusters:
        result.append({
            "id": c["id"],
            "name": c["name"] or "",
            "face_count": c["face_count"],
            "rep_face_id": c["rep_face_id"],
        })

    db.close()
    return jsonify(result)


@app.route("/api/faces/<int:cluster_id>")
def api_face_cluster(cluster_id):
    """해당 클러스터의 사진 목록"""
    db = get_db()
    cluster = db.execute("SELECT * FROM face_clusters WHERE id=?", (cluster_id,)).fetchone()
    if not cluster:
        db.close()
        abort(404)

    faces = db.execute(
        "SELECT f.id as face_id, f.photo_id, f.confidence, p.filepath, p.date_taken, p.camera "
        "FROM faces f JOIN photos p ON f.photo_id = p.id "
        "WHERE f.cluster_id = ? ORDER BY p.date_taken DESC",
        (cluster_id,),
    ).fetchall()

    result = {
        "cluster": {
            "id": cluster["id"],
            "name": cluster["name"] or "",
            "face_count": cluster["face_count"],
        },
        "faces": [{
            "face_id": f["face_id"],
            "photo_id": f["photo_id"],
            "confidence": f["confidence"],
            "filepath": f["filepath"],
            "filename": Path(f["filepath"]).name,
            "date_taken": f["date_taken"] or "",
            "camera": f["camera"] or "",
        } for f in faces],
    }

    db.close()
    return jsonify(result)


@app.route("/api/faces/<int:cluster_id>", methods=["PUT"])
def rename_face_cluster(cluster_id):
    """클러스터 이름 변경"""
    db = get_db()
    cluster = db.execute("SELECT id FROM face_clusters WHERE id=?", (cluster_id,)).fetchone()
    if not cluster:
        db.close()
        abort(404)

    data = request.get_json()
    name = (data.get("name") or "").strip()
    db.execute("UPDATE face_clusters SET name=? WHERE id=?", (name, cluster_id))
    db.commit()
    db.close()
    return jsonify({"id": cluster_id, "name": name})


@app.route("/face-thumb/<int:face_id>")
def serve_face_thumb(face_id):
    """얼굴 크롭 썸네일"""
    db = get_db()
    face = db.execute(
        "SELECT f.*, p.filepath FROM faces f JOIN photos p ON f.photo_id = p.id WHERE f.id=?",
        (face_id,),
    ).fetchone()
    db.close()

    if not face:
        abort(404)

    p = Path(face["filepath"])
    if not p.exists():
        abort(404)

    FACE_THUMB_DIR.mkdir(exist_ok=True)
    thumb_path = FACE_THUMB_DIR / f"{face_id}.jpg"

    if not thumb_path.exists():
        try:
            from PIL import Image
            img = Image.open(p)
            w, h = img.size
            # 정규화 좌표 → 픽셀 좌표 (여유 마진 포함)
            margin = 0.3
            bx = face["bbox_x"] * w
            by = face["bbox_y"] * h
            bw = face["bbox_w"] * w
            bh = face["bbox_h"] * h
            mx = bw * margin
            my = bh * margin
            left = max(0, int(bx - mx))
            top_ = max(0, int(by - my))
            right = min(w, int(bx + bw + mx))
            bottom = min(h, int(by + bh + my))
            crop = img.crop((left, top_, right, bottom))
            crop.thumbnail((200, 200))
            if crop.mode in ("RGBA", "P"):
                crop = crop.convert("RGB")
            crop.save(thumb_path, "JPEG", quality=85)
        except Exception:
            # 크롭 실패 시 전체 썸네일 반환
            THUMB_DIR.mkdir(exist_ok=True)
            fallback = THUMB_DIR / f"{face['photo_id']}.jpg"
            if fallback.exists():
                return send_file(fallback)
            return send_file(p)

    return send_file(thumb_path)


# ── Photo / Thumbnail serving ────────────────────────

@app.route("/photo/<int:photo_id>")
def serve_photo(photo_id):
    db = get_db()
    row = db.execute("SELECT filepath FROM photos WHERE id=?", (photo_id,)).fetchone()
    db.close()
    if not row:
        abort(404)
    p = Path(row["filepath"])
    if not p.exists():
        abort(404)
    return send_file(p)


@app.route("/thumb/<int:photo_id>")
def serve_thumb(photo_id):
    db = get_db()
    row = db.execute("SELECT filepath FROM photos WHERE id=?", (photo_id,)).fetchone()
    db.close()
    if not row:
        abort(404)
    p = Path(row["filepath"])
    if not p.exists():
        abort(404)

    THUMB_DIR.mkdir(exist_ok=True)
    thumb_path = THUMB_DIR / f"{photo_id}.jpg"

    if not thumb_path.exists():
        try:
            from PIL import Image
            img = Image.open(p)
            img.thumbnail((400, 400))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(thumb_path, "JPEG", quality=80)
        except Exception:
            return send_file(p)

    return send_file(thumb_path)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5555, debug=False)

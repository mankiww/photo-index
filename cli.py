#!/usr/bin/env python3
"""photo-index: 라즈베리파이 이미지 인덱싱 시스템

사용법:
  photo-index add <name> <path>       경로 등록
  photo-index remove <name>           경로 삭제
  photo-index paths                   등록된 경로 목록
  photo-index scan <name|all|path>    스캔 (미태깅만)
  photo-index scan-faces <name|all|path>  얼굴 감지 + 임베딩 추출
  photo-index cluster-faces           얼굴 클러스터링 (DBSCAN)
  photo-index faces                   얼굴 클러스터 목록
  photo-index search [옵션]           검색
  photo-index status                  통계
  photo-index web start               웹서버 시작
  photo-index web stop                웹서버 중지
  photo-index web restart             웹서버 재시작
  photo-index web status              웹서버 상태 확인
"""

import argparse
import configparser
import json
import os
import re
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path

# ── 상수 ──────────────────────────────────────────────
CONF_PATH = Path(os.environ.get("PHOTO_INDEX_CONF", Path.home() / ".photo-indexer.conf"))
DB_PATH = Path(os.environ.get("PHOTO_INDEX_DB", Path.home() / ".photo-indexer.db"))
YOLO_MODEL_PATH = Path(os.environ.get("PHOTO_INDEX_YOLO_MODEL", Path.home() / "yolov8n.pt"))
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif", ".webp"}
YOLO_CONF_THRESHOLD = float(os.environ.get("PHOTO_INDEX_YOLO_CONF", "0.3"))

# ── 설정 파일 ─────────────────────────────────────────

def load_config():
    config = configparser.ConfigParser()
    if CONF_PATH.exists():
        config.read(CONF_PATH)
    if "paths" not in config:
        config["paths"] = {}
    return config

def save_config(config):
    with open(CONF_PATH, "w") as f:
        config.write(f)

# ── DB ────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS photos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            filepath    TEXT UNIQUE NOT NULL,
            source_name TEXT,
            date_taken  TEXT,
            gps_lat     REAL,
            gps_lon     REAL,
            camera      TEXT,
            lens        TEXT,
            iso         INTEGER,
            yolo_tags   TEXT,
            indexed_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_filepath ON photos(filepath)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_source ON photos(source_name)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_date ON photos(date_taken)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_camera ON photos(camera)")

    # 마이그레이션: custom_tags 컬럼
    cols = [r[1] for r in db.execute("PRAGMA table_info(photos)").fetchall()]
    if "custom_tags" not in cols:
        db.execute("ALTER TABLE photos ADD COLUMN custom_tags TEXT DEFAULT '[]'")

    # 얼굴 클러스터 테이블
    db.execute("""
        CREATE TABLE IF NOT EXISTS face_clusters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT DEFAULT '',
            face_count  INTEGER DEFAULT 0,
            rep_face_id INTEGER
        )
    """)

    # 얼굴 임베딩 테이블
    db.execute("""
        CREATE TABLE IF NOT EXISTS faces (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_id    INTEGER NOT NULL,
            face_index  INTEGER DEFAULT 0,
            embedding   BLOB NOT NULL,
            bbox_x      REAL, bbox_y REAL,
            bbox_w      REAL, bbox_h REAL,
            confidence  REAL,
            cluster_id  INTEGER,
            FOREIGN KEY(photo_id) REFERENCES photos(id),
            FOREIGN KEY(cluster_id) REFERENCES face_clusters(id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id)")

    db.commit()
    return db

def is_indexed(db, filepath):
    row = db.execute("SELECT 1 FROM photos WHERE filepath=?", (filepath,)).fetchone()
    return row is not None

# ── EXIF ──────────────────────────────────────────────

def extract_exif(filepath):
    try:
        result = subprocess.run(
            [
                "exiftool", "-j",
                "-DateTimeOriginal",
                "-GPSLatitude#",
                "-GPSLongitude#",
                "-Model",
                "-LensModel",
                "-ISO",
                str(filepath),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        if not data:
            return {}
        d = data[0]
        return {
            "date_taken": d.get("DateTimeOriginal", ""),
            "gps_lat": d.get("GPSLatitude") if d.get("GPSLatitude") else None,
            "gps_lon": d.get("GPSLongitude") if d.get("GPSLongitude") else None,
            "camera": d.get("Model", ""),
            "lens": d.get("LensModel", ""),
            "iso": d.get("ISO"),
        }
    except Exception as e:
        print(f"  EXIF 오류 ({filepath}): {e}", file=sys.stderr)
        return {}

# ── YOLO ──────────────────────────────────────────────

_yolo_model = None

def get_yolo_model():
    global _yolo_model
    if _yolo_model is None:
        from ultralytics import YOLO
        _yolo_model = YOLO(str(YOLO_MODEL_PATH))
    return _yolo_model

def detect_tags(filepath):
    try:
        model = get_yolo_model()
        results = model(str(filepath), verbose=False, conf=YOLO_CONF_THRESHOLD)
        tags = set()
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                tags.add(model.names[cls_id])
        return sorted(tags)
    except Exception as e:
        print(f"  YOLO 오류 ({filepath}): {e}", file=sys.stderr)
        return []

# ── 스캔 로직 ─────────────────────────────────────────

def find_images(root_path):
    root = Path(root_path)
    if not root.exists():
        print(f"오류: 경로가 존재하지 않습니다: {root}", file=sys.stderr)
        return
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS and not p.name.startswith("._"):
            yield p

def scan_path(db, path, source_name=None):
    total = 0
    skipped = 0
    indexed = 0
    errors = 0

    images = list(find_images(path))
    total = len(images)

    if total == 0:
        print(f"이미지 파일 없음: {path}")
        return

    print(f"스캔 대상: {path}")
    print(f"총 이미지: {total}개")
    print()

    # YOLO 모델 미리 로드
    print("YOLO 모델 로딩 중...")
    t0 = time.time()
    get_yolo_model()
    print(f"YOLO 모델 준비 완료 ({time.time()-t0:.1f}s)\n")

    for i, filepath in enumerate(images, 1):
        fp_str = str(filepath)

        if is_indexed(db, fp_str):
            skipped += 1
            continue

        pct = i * 100 // total
        print(f"[{pct:3d}%] ({i}/{total}) {filepath.name}", end="", flush=True)

        # EXIF 추출
        exif = extract_exif(filepath)

        # YOLO 태깅
        tags = detect_tags(filepath)
        tags_json = json.dumps(tags, ensure_ascii=False) if tags else "[]"

        # DB 저장
        try:
            db.execute(
                """INSERT OR IGNORE INTO photos
                   (filepath, source_name, date_taken, gps_lat, gps_lon, camera, lens, iso, yolo_tags)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fp_str,
                    source_name or "",
                    exif.get("date_taken", ""),
                    exif.get("gps_lat"),
                    exif.get("gps_lon"),
                    exif.get("camera", ""),
                    exif.get("lens", ""),
                    exif.get("iso"),
                    tags_json,
                ),
            )
            db.commit()
            indexed += 1
            tag_str = ", ".join(tags) if tags else "-"
            print(f"  tags=[{tag_str}]")
        except Exception as e:
            errors += 1
            print(f"  DB 오류: {e}", file=sys.stderr)

    print(f"\n완료: 신규 {indexed} / 스킵 {skipped} / 에러 {errors} / 전체 {total}")

    if indexed > 0:
        apply_schedule_tags(db)


def apply_schedule_tags(db):
    """기존 스케줄에 매칭되는 사진에 자동 태그 적용"""
    # schedules 테이블이 없으면 스킵
    has_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schedules'"
    ).fetchone()
    if not has_table:
        return

    schedules = db.execute("SELECT * FROM schedules").fetchall()
    if not schedules:
        return

    total_tagged = 0
    for s in schedules:
        date_like = s["date"].replace("-", ":") + "%"
        conditions = ["date_taken LIKE ?"]
        params = [date_like]

        if s["time_start"] and s["time_end"]:
            conditions.append("substr(date_taken, 12, 5) >= ?")
            conditions.append("substr(date_taken, 12, 5) <= ?")
            params.extend([s["time_start"], s["time_end"]])
        elif s["time_start"]:
            conditions.append("substr(date_taken, 12, 5) >= ?")
            params.append(s["time_start"])
        elif s["time_end"]:
            conditions.append("substr(date_taken, 12, 5) <= ?")
            params.append(s["time_end"])

        where = " AND ".join(conditions)
        rows = db.execute(f"SELECT id, custom_tags FROM photos WHERE {where}", params).fetchall()

        count = 0
        for r in rows:
            tags = json.loads(r["custom_tags"] or "[]")
            if s["title"] not in tags:
                tags.append(s["title"])
                db.execute("UPDATE photos SET custom_tags=? WHERE id=?",
                           (json.dumps(tags, ensure_ascii=False), r["id"]))
                count += 1

        if count > 0:
            db.execute("UPDATE schedules SET tagged_count=tagged_count+? WHERE id=?", (count, s["id"]))
            total_tagged += count

    if total_tagged > 0:
        db.commit()
        print(f"\n스케줄 자동태깅: {total_tagged}장에 태그 추가됨")


# ── 얼굴 감지/임베딩 ─────────────────────────────────

def extract_faces(filepath):
    """DeepFace로 얼굴 감지 + ArcFace 임베딩 추출. 리스트[{embedding, bbox, confidence}] 반환"""
    try:
        from deepface import DeepFace
        from PIL import Image
        img = Image.open(filepath)
        img_w, img_h = img.size

        results = DeepFace.represent(
            img_path=str(filepath),
            model_name="ArcFace",
            detector_backend="retinaface",
            enforce_detection=False,
        )

        faces = []
        for i, r in enumerate(results):
            emb = r.get("embedding")
            if not emb:
                continue
            region = r.get("facial_area", {})
            x = region.get("x", 0) / img_w
            y = region.get("y", 0) / img_h
            w = region.get("w", 0) / img_w
            h = region.get("h", 0) / img_h
            conf = r.get("face_confidence", 0.0)
            if conf < 0.5:
                continue
            emb_blob = struct.pack(f"{len(emb)}f", *emb)
            faces.append({
                "face_index": i,
                "embedding": emb_blob,
                "bbox_x": x, "bbox_y": y,
                "bbox_w": w, "bbox_h": h,
                "confidence": conf,
            })
        return faces
    except Exception as e:
        print(f"  얼굴 감지 오류 ({filepath}): {e}", file=sys.stderr)
        return []

def scan_faces_for_path(db, path, source_name=None):
    """주어진 경로의 인덱싱된 사진에서 얼굴 추출 (이미 추출된 사진은 건너뜀)"""
    if source_name:
        rows = db.execute(
            "SELECT id, filepath FROM photos WHERE source_name=?", (source_name,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, filepath FROM photos WHERE filepath LIKE ?", (str(path) + "%",)
        ).fetchall()

    total = len(rows)
    if total == 0:
        print("대상 사진 없음")
        return

    # 이미 얼굴 추출된 photo_id 집합
    done_ids = set(
        r[0] for r in db.execute("SELECT DISTINCT photo_id FROM faces").fetchall()
    )

    pending = [(r["id"], r["filepath"]) for r in rows if r["id"] not in done_ids]
    print(f"얼굴 스캔 대상: {len(pending)}개 (전체 {total}, 스킵 {total - len(pending)})")

    if not pending:
        print("모든 사진이 이미 처리됨")
        return

    extracted = 0
    face_total = 0
    for i, (photo_id, filepath) in enumerate(pending, 1):
        pct = i * 100 // len(pending)
        print(f"[{pct:3d}%] ({i}/{len(pending)}) {Path(filepath).name}", end="", flush=True)

        faces = extract_faces(filepath)
        for f in faces:
            db.execute(
                """INSERT INTO faces (photo_id, face_index, embedding, bbox_x, bbox_y, bbox_w, bbox_h, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (photo_id, f["face_index"], f["embedding"],
                 f["bbox_x"], f["bbox_y"], f["bbox_w"], f["bbox_h"], f["confidence"]),
            )
        db.commit()

        n = len(faces)
        face_total += n
        if n > 0:
            extracted += 1
        print(f"  faces={n}")

    print(f"\n완료: {extracted}장에서 얼굴 {face_total}개 추출")

def cmd_scan_faces(args):
    config = load_config()
    db = get_db()
    target = args.target

    if target == "all":
        if not config["paths"]:
            print("등록된 경로 없음.", file=sys.stderr)
            sys.exit(1)
        for name, path in config["paths"].items():
            print(f"\n{'='*60}")
            print(f"소스: {name}")
            print(f"{'='*60}")
            scan_faces_for_path(db, path, source_name=name)
    elif target in config["paths"]:
        path = config["paths"][target]
        scan_faces_for_path(db, path, source_name=target)
    elif os.path.isdir(target):
        scan_faces_for_path(db, target)
    else:
        print(f"오류: '{target}'은 등록된 이름도, 존재하는 경로도 아닙니다.", file=sys.stderr)
        sys.exit(1)

def cmd_cluster_faces(args):
    """DBSCAN으로 얼굴 클러스터링"""
    import numpy as np
    from sklearn.cluster import DBSCAN
    from sklearn.metrics.pairwise import cosine_distances

    db = get_db()
    rows = db.execute("SELECT id, embedding FROM faces").fetchall()

    if len(rows) < 2:
        print(f"얼굴 데이터가 부족합니다 ({len(rows)}개). 최소 2개 필요.")
        return

    print(f"전체 얼굴: {len(rows)}개")
    print("임베딩 로드 중...")

    face_ids = []
    embeddings = []
    for r in rows:
        face_ids.append(r["id"])
        n_floats = len(r["embedding"]) // 4
        emb = struct.unpack(f"{n_floats}f", r["embedding"])
        embeddings.append(emb)

    X = np.array(embeddings)

    print("Cosine distance 계산 중...")
    dist_matrix = cosine_distances(X)

    print("DBSCAN 클러스터링 중...")
    clustering = DBSCAN(eps=0.45, min_samples=2, metric="precomputed")
    labels = clustering.fit_predict(dist_matrix)

    # 기존 클러스터 초기화
    db.execute("DELETE FROM face_clusters")
    db.execute("UPDATE faces SET cluster_id = NULL")

    # 클러스터별 처리
    unique_labels = set(labels)
    unique_labels.discard(-1)

    cluster_map = {}
    for label in sorted(unique_labels):
        indices = [i for i, l in enumerate(labels) if l == label]
        face_id_list = [face_ids[i] for i in indices]

        # 대표 얼굴: confidence가 가장 높은 것
        rep = db.execute(
            f"SELECT id FROM faces WHERE id IN ({','.join('?' * len(face_id_list))}) ORDER BY confidence DESC LIMIT 1",
            face_id_list,
        ).fetchone()

        cursor = db.execute(
            "INSERT INTO face_clusters (name, face_count, rep_face_id) VALUES ('', ?, ?)",
            (len(face_id_list), rep["id"]),
        )
        cluster_id = cursor.lastrowid
        cluster_map[label] = cluster_id

        db.execute(
            f"UPDATE faces SET cluster_id = ? WHERE id IN ({','.join('?' * len(face_id_list))})",
            [cluster_id] + face_id_list,
        )

    db.commit()

    noise_count = sum(1 for l in labels if l == -1)
    print(f"\n완료: {len(unique_labels)}개 클러스터 생성, 미분류 {noise_count}개")

def cmd_faces(args):
    """클러스터 목록 출력"""
    db = get_db()

    total_faces = db.execute("SELECT COUNT(*) as c FROM faces").fetchone()["c"]
    clusters = db.execute(
        "SELECT * FROM face_clusters ORDER BY face_count DESC"
    ).fetchall()

    print(f"전체 얼굴: {total_faces}개")
    print(f"클러스터: {len(clusters)}개\n")

    if not clusters:
        print("클러스터 없음. 'photo-index cluster-faces'를 먼저 실행하세요.")
        return

    for c in clusters:
        name = c["name"] or "(미명명)"
        print(f"  #{c['id']:3d}  {name:20s}  얼굴 {c['face_count']}개  대표={c['rep_face_id']}")

    noise = db.execute("SELECT COUNT(*) as c FROM faces WHERE cluster_id IS NULL").fetchone()["c"]
    if noise:
        print(f"\n  미분류: {noise}개")

# ── 검색 ──────────────────────────────────────────────

def cmd_search(args):
    db = get_db()
    conditions = []
    params = []

    if args.tag:
        # JSON 배열에서 태그 검색
        conditions.append("yolo_tags LIKE ?")
        params.append(f'%"{args.tag}"%')
    if args.camera:
        conditions.append("camera LIKE ?")
        params.append(f"%{args.camera}%")
    if args.lens:
        conditions.append("lens LIKE ?")
        params.append(f"%{args.lens}%")
    if args.date:
        # YYYY-MM-DD 또는 YYYY-MM 또는 YYYY
        conditions.append("date_taken LIKE ?")
        params.append(f"{args.date.replace('-', ':')}%")
    if args.source:
        conditions.append("source_name = ?")
        params.append(args.source)
    if args.gps:
        conditions.append("gps_lat IS NOT NULL AND gps_lon IS NOT NULL")

    if not conditions:
        print("검색 조건을 하나 이상 지정하세요.", file=sys.stderr)
        print("  --tag, --camera, --lens, --date, --source, --gps", file=sys.stderr)
        return

    where = " AND ".join(conditions)
    query = f"SELECT * FROM photos WHERE {where} ORDER BY date_taken"

    rows = db.execute(query, params).fetchall()

    if not rows:
        print("결과 없음")
        return

    print(f"검색 결과: {len(rows)}개\n")
    for r in rows:
        tags = json.loads(r["yolo_tags"]) if r["yolo_tags"] else []
        tag_str = ", ".join(tags) if tags else "-"
        date = r["date_taken"] or "날짜없음"
        cam = r["camera"] or ""
        lens = r["lens"] or ""
        gps = ""
        if r["gps_lat"] and r["gps_lon"]:
            gps = f"  GPS: {r['gps_lat']:.6f}, {r['gps_lon']:.6f}"
        print(f"  {r['filepath']}")
        print(f"    [{r['source_name'] or '-'}] {date} | {cam} {lens} ISO{r['iso'] or '?'}")
        print(f"    tags: {tag_str}{gps}")
        print()

# ── CLI 명령어 ────────────────────────────────────────

def cmd_add(args):
    path = os.path.abspath(args.path)
    if not os.path.isdir(path):
        print(f"오류: 디렉토리가 존재하지 않습니다: {path}", file=sys.stderr)
        sys.exit(1)
    config = load_config()
    config["paths"][args.name] = path
    save_config(config)
    print(f"등록: {args.name} → {path}")

def cmd_remove(args):
    config = load_config()
    if args.name not in config["paths"]:
        print(f"오류: '{args.name}'이 등록되어 있지 않습니다.", file=sys.stderr)
        sys.exit(1)
    del config["paths"][args.name]
    save_config(config)
    print(f"삭제: {args.name}")

def cmd_paths(args):
    config = load_config()
    if not config["paths"]:
        print("등록된 경로 없음. 'photo-index add <name> <path>'로 추가하세요.")
        return
    print("등록된 경로:")
    for name, path in config["paths"].items():
        exists = "OK" if os.path.isdir(path) else "NOT FOUND"
        print(f"  {name:15s} → {path}  [{exists}]")

def cmd_scan(args):
    config = load_config()
    db = get_db()
    target = args.target

    if target == "all":
        if not config["paths"]:
            print("등록된 경로 없음.", file=sys.stderr)
            sys.exit(1)
        for name, path in config["paths"].items():
            print(f"\n{'='*60}")
            print(f"소스: {name}")
            print(f"{'='*60}")
            scan_path(db, path, source_name=name)
    elif target in config["paths"]:
        path = config["paths"][target]
        scan_path(db, path, source_name=target)
    elif os.path.isdir(target):
        scan_path(db, target, source_name=None)
    else:
        print(f"오류: '{target}'은 등록된 이름도, 존재하는 경로도 아닙니다.", file=sys.stderr)
        sys.exit(1)

def cmd_status(args):
    db = get_db()
    config = load_config()

    total = db.execute("SELECT COUNT(*) as c FROM photos").fetchone()["c"]
    tagged = db.execute("SELECT COUNT(*) as c FROM photos WHERE yolo_tags != '[]' AND yolo_tags != ''").fetchone()["c"]
    untagged = total - tagged

    print(f"전체 인덱싱: {total}개")
    print(f"태그 있음:   {tagged}개")
    print(f"태그 없음:   {untagged}개")

    if config["paths"]:
        print(f"\n소스별 통계:")
        for name in config["paths"]:
            cnt = db.execute("SELECT COUNT(*) as c FROM photos WHERE source_name=?", (name,)).fetchone()["c"]
            print(f"  {name:15s}: {cnt}개")

    # 상위 태그
    rows = db.execute("SELECT yolo_tags FROM photos WHERE yolo_tags != '[]'").fetchall()
    if rows:
        tag_counts = {}
        for r in rows:
            for t in json.loads(r["yolo_tags"]):
                tag_counts[t] = tag_counts.get(t, 0) + 1
        top = sorted(tag_counts.items(), key=lambda x: -x[1])[:15]
        print(f"\n상위 태그:")
        for tag, count in top:
            print(f"  {tag:20s}: {count}개")

# ── 웹서버 관리 ──────────────────────────────────────

WEB_DIR = Path(os.environ.get("PHOTO_INDEX_WEB_DIR", Path.home() / "photo-indexer-web"))
WEB_SCREEN = "photo-web"
WEB_PORT = int(os.environ.get("PHOTO_INDEX_WEB_PORT", "5555"))


def _web_is_running():
    """screen 세션 + 포트 리스닝 확인"""
    try:
        result = subprocess.run(
            ["screen", "-ls"], capture_output=True, text=True
        )
        if WEB_SCREEN not in result.stdout:
            return False
        # 포트 확인
        result2 = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True
        )
        return f":{WEB_PORT}" in result2.stdout
    except Exception:
        return False


def cmd_web(args):
    action = args.action

    if action == "start":
        if _web_is_running():
            print(f"웹서버가 이미 실행 중입니다 (http://localhost:{WEB_PORT})")
            return

        app_py = WEB_DIR / "app.py"
        if not app_py.exists():
            print(f"오류: {app_py} 파일을 찾을 수 없습니다.", file=sys.stderr)
            sys.exit(1)

        python = sys.executable
        subprocess.run([
            "screen", "-dmS", WEB_SCREEN,
            python, str(app_py),
        ], cwd=str(WEB_DIR))

        # 시작 대기
        for _ in range(10):
            time.sleep(0.5)
            if _web_is_running():
                break

        if _web_is_running():
            print(f"웹서버 시작됨: http://localhost:{WEB_PORT}")
        else:
            print("웹서버 시작 실패. 로그를 확인하세요.", file=sys.stderr)
            sys.exit(1)

    elif action == "stop":
        if not _web_is_running():
            print("웹서버가 실행 중이 아닙니다.")
            return

        subprocess.run(["screen", "-S", WEB_SCREEN, "-X", "quit"])
        time.sleep(1)
        print("웹서버 중지됨")

    elif action == "restart":
        if _web_is_running():
            subprocess.run(["screen", "-S", WEB_SCREEN, "-X", "quit"])
            time.sleep(1)
            print("기존 서버 중지됨")

        # start 로직 재사용
        args.action = "start"
        cmd_web(args)

    elif action == "status":
        if _web_is_running():
            print(f"웹서버 실행 중: http://localhost:{WEB_PORT}")
        else:
            print("웹서버 중지 상태")

    else:
        print(f"알 수 없는 액션: {action}", file=sys.stderr)
        print("사용법: photo-index web [start|stop|restart|status]")
        sys.exit(1)


# ── 메인 ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="photo-index",
        description="라즈베리파이 이미지 인덱싱 시스템",
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="경로 등록")
    p_add.add_argument("name", help="소스 이름 (예: a7m3)")
    p_add.add_argument("path", help="디렉토리 경로")

    # remove
    p_rm = sub.add_parser("remove", help="경로 삭제")
    p_rm.add_argument("name", help="소스 이름")

    # paths
    sub.add_parser("paths", help="등록된 경로 목록")

    # scan
    p_scan = sub.add_parser("scan", help="스캔 (미태깅만)")
    p_scan.add_argument("target", help="소스 이름, 'all', 또는 직접 경로")

    # search
    p_search = sub.add_parser("search", help="검색")
    p_search.add_argument("--tag", help="YOLO 태그 (예: person, dog)")
    p_search.add_argument("--camera", help="카메라 모델 (부분일치)")
    p_search.add_argument("--lens", help="렌즈 모델 (부분일치)")
    p_search.add_argument("--date", help="날짜 (YYYY, YYYY-MM, YYYY-MM-DD)")
    p_search.add_argument("--source", help="소스 이름")
    p_search.add_argument("--gps", action="store_true", help="GPS 정보 있는 것만")

    # scan-faces
    p_sf = sub.add_parser("scan-faces", help="얼굴 감지 + 임베딩 추출")
    p_sf.add_argument("target", help="소스 이름, 'all', 또는 직접 경로")

    # cluster-faces
    sub.add_parser("cluster-faces", help="얼굴 클러스터링 (DBSCAN)")

    # faces
    sub.add_parser("faces", help="얼굴 클러스터 목록")

    # status
    sub.add_parser("status", help="통계")

    # web
    p_web = sub.add_parser("web", help="웹서버 관리 (start/stop/restart/status)")
    p_web.add_argument("action", choices=["start", "stop", "restart", "status"],
                        help="start: 시작, stop: 중지, restart: 재시작, status: 상태확인")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "add": cmd_add,
        "remove": cmd_remove,
        "paths": cmd_paths,
        "scan": cmd_scan,
        "scan-faces": cmd_scan_faces,
        "cluster-faces": cmd_cluster_faces,
        "faces": cmd_faces,
        "search": cmd_search,
        "status": cmd_status,
        "web": cmd_web,
    }
    commands[args.command](args)

if __name__ == "__main__":
    main()

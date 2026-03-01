# photo-index

라즈베리파이에서 돌아가는 로컬 사진 인덱싱 시스템.

EXIF 메타데이터 추출, YOLOv8 자동 피사체 태깅, DeepFace 얼굴 클러스터링, 웹 검색 UI를 제공한다.

## 주요 기능

- **EXIF 추출** — 카메라, 렌즈, ISO, GPS, 촬영일
- **YOLO 자동 태깅** — YOLOv8 Nano로 피사체 감지 (person, car, dog 등 80종)
- **커스텀 태그** — 웹 UI에서 수동 태그 추가/삭제
- **얼굴 클러스터링** — DeepFace(RetinaFace + ArcFace) 임베딩 추출 → DBSCAN 자동 그룹화 → 이름 붙이기
- **웹 검색 UI** — 태그/카메라/렌즈/날짜/소스 필터 드롭다운, 라이트박스, 얼굴 갤러리

## 요구사항

- Python 3.10+
- exiftool (`sudo apt install libimage-exiftool-perl`)

## 설치

```bash
git clone https://github.com/mankiww/photo-index.git
cd photo-index

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

YOLO 모델 다운로드:

```bash
# 홈 디렉토리 또는 PHOTO_INDEX_YOLO_MODEL 경로에 배치
wget https://github.com/ultralytics/assets/releases/download/v8.1.0/yolov8n.pt -O ~/yolov8n.pt
```

## 사용법

### CLI

```bash
# 경로 등록
python cli.py add a7m3 /path/to/photos

# 사진 스캔 (EXIF + YOLO 태깅)
python cli.py scan a7m3
python cli.py scan all          # 등록된 전체 경로

# 얼굴 감지 + 임베딩 추출
python cli.py scan-faces a7m3

# 얼굴 클러스터링
python cli.py cluster-faces

# 클러스터 목록
python cli.py faces

# 검색
python cli.py search --tag person --camera ILCE-7M3
python cli.py search --date 2024-07 --lens "50mm"

# 통계
python cli.py status
```

### 웹 서버

```bash
python app.py
# http://localhost:5555
```

#### 웹 기능

- **Photos 탭** — 필터 드롭다운(태그/카메라/렌즈/날짜) 자동완성 검색, 썸네일 그리드, 라이트박스
- **Faces 탭** — 인물별 얼굴 썸네일 그리드, 더블클릭으로 이름 변경, 클릭 시 해당 인물 사진 검색
- **태그 편집** — 라이트박스에서 커스텀 태그 추가/삭제 (YOLO 태그는 읽기전용)

## 환경변수

모두 선택사항. 미설정 시 기본값 사용.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `PHOTO_INDEX_DB` | `~/.photo-indexer.db` | SQLite DB 경로 |
| `PHOTO_INDEX_CONF` | `~/.photo-indexer.conf` | 설정 파일 경로 |
| `PHOTO_INDEX_YOLO_MODEL` | `~/yolov8n.pt` | YOLO 모델 경로 |
| `PHOTO_INDEX_YOLO_CONF` | `0.3` | YOLO 신뢰도 임계값 |
| `PHOTO_INDEX_THUMBS` | `~/.photo-indexer-thumbs` | 썸네일 캐시 |
| `PHOTO_INDEX_FACE_THUMBS` | `~/.photo-indexer-face-thumbs` | 얼굴 썸네일 캐시 |

## API

| 엔드포인트 | 메서드 | 설명 |
|---|---|---|
| `/api/stats` | GET | 전체 통계 |
| `/api/filters` | GET | 필터 옵션 목록 (태그/카메라/렌즈/소스/날짜) |
| `/api/search` | GET | 사진 검색 |
| `/api/tags` | GET | 태그 목록 |
| `/api/photos/<id>/tags` | POST | 커스텀 태그 추가 |
| `/api/photos/<id>/tags` | DELETE | 커스텀 태그 삭제 |
| `/api/faces` | GET | 얼굴 클러스터 목록 |
| `/api/faces/<id>` | GET | 클러스터 상세 (사진 목록) |
| `/api/faces/<id>` | PUT | 클러스터 이름 변경 |
| `/photo/<id>` | GET | 원본 사진 |
| `/thumb/<id>` | GET | 썸네일 (400px) |
| `/face-thumb/<id>` | GET | 얼굴 크롭 썸네일 |

## 프로젝트 구조

```
photo-index/
├── cli.py              # CLI 도구 (스캔, 검색, 얼굴 처리)
├── app.py              # Flask 웹 서버
├── static/
│   └── index.html      # 프론트엔드 (SPA)
├── requirements.txt
├── .env.example
└── .gitignore
```

## License

MIT

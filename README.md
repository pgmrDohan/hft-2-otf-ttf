# HFT 2 OTF or TTF

한컴오피스(한글) 설치본에 들어 있는 **HFT 폰트**("Han Unified Font File 1.0")를
OpenType(.otf, CFF 곡선) 과 TrueType(.ttf) 으로 변환하는 도구.

## 사용법

```bash
pip3 install fonttools skia-pathops     # skia-pathops 는 선택(겹친 윤곽 정리)
python3 hft2font.py <HFT파일 또는 폴더> -o <출력폴더> [-f otf|ttf|both] [--merge] [-j N]
```

* `-f` : 출력 형식 (기본 both)
* `--merge` : 같은 서체 이름의 언어별 파일(한글/영문/한자/일어/특수문자)을 하나로 합친
  "통합 폰트"도 추가로 만듭니다 (예: TESMJHG+TESMJEN+TESMJJP → `신명 신명조.otf`).
* `--code-dir` : 한컴 코드표(`Resources/Hnc/Bin/Code/u300_*.dat`) 위치.
  입력이 앱 번들 안의 `…/Hnc/Shared/Fonts` 이면 자동으로 찾습니다.
* `--only A,B,C` : 일부 파일만 변환
* `--skip-existing` / `--progress FILE` : 중단 후 이어서 변환

## HFT 포맷 요약 (분석 결과)

| 위치 | 내용 |
|---|---|
| 0x000 | 시그니처 `Han Unified Font File 1.0\x1a\x04\x03\x02\x01\xff\x00` |
| 0x02c / 0x06c / 0x08c | 서체 이름들 (조합형 Johab 인코딩) |
| 0x134 | 드라이버 문자열: 없음 / `Hangul Mapsi font for HWP 2.5` / `Hanyang outline font for HWP 2.1` / `Human Font for HWP 2.1` |
| 0x17a | 글자폭 기준 em |
| 0x1a4.. | 폭 블록 수, 외곽선 세트 수, 비트맵 세트 수, 각 오프셋 |

* **글리프 세트 → 섹션**: 섹션마다 코드 범위와 매핑 방식이 있음
  (0=연속 범위, 1=정렬된 코드 목록/KS X 1001 2350자 표, 2·4=초·중·종성 조합형, 3=옛한글 자모).
* **글리프 레코드**: `[bbox 4×s16] u16 길이 + 글리프 프로그램`.
  프로그램은 Type1과 비슷한 상대좌표 명령어(1–3 moveto, 5–7 lineto, 9–11 curveto,
  0/4 closepath, 0x20–0x44 힌트)이며, closepath 뒤 현재점은 시작점으로 돌아갑니다.
* **암호화**: Mapsi·Hanyang 드라이버는 16비트 LCG 스트림 암호
  (`p = c ^ (r>>8); r = (c + r)*0xC73E + K`, 초기값/상수가 드라이버별로 다름),
  Human(HCI) 드라이버는 헤더의 키로 XOR.
* **좌표계**: em 박스 아래쪽이 y=0, HWP는 em의 80%/20%를 ascent/descent로 씀
  → 변환 시 y에서 0.2em을 빼서 베이스라인을 맞춤 (UPM 1000).
* **문자 코드**: HWP 내부 코드(0x8000~ 조합형 한글, 0x4000~ 한자, 그 외 기호)를
  한컴 코드표(u300_gen/cjk/com/pua/u2h/h2u.dat)로 유니코드에 매핑합니다.
  유니코드 대응이 없는 글리프는 `U+F0000 + HWP코드`(보충 사용자 영역)에 넣었습니다.

## 참고

* 외곽선이 없는 **비트맵 전용 HFT**(HGMJ, HJMJ, ENMJ, JPMJ, *SYS, HY*GL 등 18개)는
  가장 큰 비트맵(대개 42px)을 픽셀 윤곽으로 그대로 벡터화했습니다(도트 모양 폰트).
* 조합형 한글 폰트는 11,172자 + 옛한글 조합까지 미리 조합해 완성형 글리프로 만듭니다.

## 이번 변환 결과

* `OTF/`, `TTF/` : HFT 387개 각각 1:1 변환 (외곽선 369개 + 비트맵 벡터화 18개)
* `merged/OTF`, `merged/TTF` : 같은 서체의 언어별 파일을 합친 통합 폰트 82종
  (예: `신명 신명조.otf` = TESMJHG + TESMJEN + TESMJJP)
* 이름 규칙: 개별 폰트는 영문 패밀리명 = 파일명(예: TESMJHG), 한국어 패밀리명 = `신명 신명조 (TESMJHG)`.
  통합 폰트는 한국어 패밀리명 = 서체 이름, 영문 = `<대표파일> Combined`.
* `convert.log` : 파일별 글리프 수 / 유니코드 매핑 수

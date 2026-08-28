# ad_connector_composer

Генератор линейных footprint'ов для **Altium Designer** из фрагментов библиотеки.

```
ad_connector_composer/
├── ad_connector_composer.exe   ← запуск (GUI)
├── README.md
└── src/
    ├── ad_connector_composer.py
    ├── requirements.txt
    └── build_exe.bat
```

## Требования к .PcbLib

Внутри должны быть **4 фрагмента** с окончаниями:

| Окончание | Роль |
|-----------|------|
| `…BASE` | шелк / ключ / designator (без pads; 3D опционален) |
| `…BEGIN` | левый контакт `begin-1`, `begin-2`, … (по числу рядов R) + 3D |
| `…PIN` | средний контакт `pin-1`, `pin-2`, … + 3D |
| `…END` | правый контакт `end-1`, `end-2`, … + 3D |

При **R = 1** достаточно одного пада (`begin-1`, `pin-1`, `end-1`).
При **R > 1** — по одному паду на ряд (`-1`, `-2`, `-3`, …).

**Префикс** — полный stem набора, например `PLS-2.54`:
ищет `PLS-2.54-BASE` … `PLS-2.54-END` и генерирует имена `PLS-2.54-7P`.

**Шаг копирования (pitch)** — только расстояние между колонками pads (mm);
в поиск фрагментов и имена не входит.

**Имя footprint:** `{prefix}-{C×R}P` — число pads с суффиксом `P`
(7 колонок × 2 ряда → `PLS-2.54-14P`, 7×1 → `PLS-2.54-7P`).

**Нумерация pads** (по умолчанию **V** — по вертикали / вниз по колонке):

| Режим | 4 колонки × 2 ряда |
|-------|---------------------|
| **V** | begin-1→1, begin-2→2, pin-1→3, pin-2→4, pin-1→5, pin-2→6, end-1→7, end-2→8 |
| **H** | begin-1→1, begin-2→5, pin-1→2, pin-2→6, pin-1→3, pin-2→7, end-1→4, end-2→8 |

Мастер **не перезаписывается**. Каждый запуск создаёт новый `.PcbLib` рядом с исходным.

## Как пользоваться

1. Запустите `ad_connector_composer.exe`
2. **Обзор…** — выберите `.PcbLib` с фрагментами
3. **Проверить фрагменты** — убедитесь, что BASE/BEGIN/PIN/END найдены
4. Задайте префикс (`PLS-2.54`), шаг копирования, диапазон **колонок**, ряды, нумерацию
5. **Сгенерировать**

Имена: `{prefix}-{C×R}P` → например `PLS-2.54-7P`, `PLD-2.54-14P`.

## Исходники

```bat
cd src
pip install -r requirements.txt
python ad_connector_composer.py
```

CLI:

```bat
python ad_connector_composer.py --gui
python ad_connector_composer.py master.PcbLib --max 10 -p 2.54 --prefix PLS-2.54
python ad_connector_composer.py master.PcbLib -c 5 -p 1.27 --prefix HDR-1.27
```

Сборка exe:

```bat
cd src
build_exe.bat
```

## Структура внутри .PcbLib

`.PcbLib` — OLE Compound File («мини-файловая система» внутри одного файла).

```
.PcbLib
├── FileHeader
└── Library/
│   ├── Data                            ← список footprint'ов
│   ├── Models/                         ← встроенные 3D (STEP)
│   │   ├── 0, 1, 2, …
│   └── ComponentParamsTOC/
│       ├── Header
│       └── Data
└── <ИмяFootprint>/
    ├── Data                            ← pads, tracks, text, ComponentBody (3D)
    ├── Parameters
    ├── WideStrings
    └── UniqueIDPrimitiveInformation/
        └── Data
```

| Уровень | Содержимое |
|---------|------------|
| **Библиотека** | Фрагменты `…BASE` / `…BEGIN` / `…PIN` / `…END` и готовые `…-N` |
| **Фрагмент** | Колонка: pads `begin-1`…`begin-R` (или `pin-` / `end-`) + `ComponentBody` |
| **BASE** | Шелк/ключ/designator; pads нет; 3D опционален |
| **Library/Models** | STEP-пул; BEGIN/PIN/END с разными MODELID; при генерации модели копируются из мастера |
| **Готовый footprint** | BASE @ 0 + BEGIN + PIN×(C−2) + END; пады `1…C×R`; имя `…-{C×R}P`; 3D на каждую колонку |

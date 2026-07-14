# sd-webui-DifferenceCFG

A mask-free, reference-scale-based global CFG re-adjustment for Forge-derived Stable Diffusion WebUIs (reForge / Forge Classic / Forge Neo).

Ported from the **Difference CFG** node in [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG). Tick a checkbox and generate — no workflow changes required.

> This extension was originally one of the modes inside `sd-webui-SkimmedCFG`. It has been split out into its own repository because it does not use SkimmedCFG's masking machinery (`get_skimming_mask` / `skimmed_CFG`) at all — it is a different family of algorithm that happens to share the same upstream author and repository. See [Background](#background) below.

---

## What it does

Standard CFG can "run away" at high scales: elements where the conditional and unconditional predictions already agree get pushed just as hard as elements where they disagree, which is part of what causes burn and oversaturation at high CFG.

Difference CFG re-blends the standard CFG output toward the output you *would* have gotten at a lower **Reference CFG**, with the blend strength driven per-element by how strongly cond and uncond already agree. Elements with high agreement are pulled back toward the calmer reference-scale result; elements with strong disagreement are left closer to the full-strength CFG output. `absolute_sum` instead computes a single global blend weight for the whole image rather than an per-element one.

This is the opposite selection target from SkimmedCFG's own masking modes (Single Scale / Replace / Linear Interpolation / Dual Scales), which target *disagreement*. The two extensions can be used together; see [Recommended usage](#recommended-usage).

---

## Features

- Registers as **"DifferenceCFG"** in the Script accordion.
- Four difference methods, faithfully ported from upstream:
  - `linear_distance`
  - `squared_distance`
  - `root_distance`
  - `absolute_sum`
- **Reference CFG** slider, range extended from the upstream default of 0–10 to **0–20**, to cover high-CFG workflows (CFG 25–30) where the reference target itself may need to sit well above the usual 5–10 range.
- **End At Percentage** gate: disables the effect after the given fraction of steps, so late-step detail is not affected.
- Backend-adaptive hook: Pre-CFG on reForge / Forge Classic, Post-CFG on Forge Neo (reads TCFG's stashed damped uncond when TCFG is enabled and stacked before it; falls back to the raw uncond otherwise).
- Full PNG infotext round-trip (send-to-txt2img / send-to-img2img preserves all settings).
- Fails safe: if the hook raises internally, standard CFG output is returned unmodified rather than breaking the generation.

---

## Installation

**Extensions → Install from URL:**

```
https://github.com/seti9585/sd-webui-DifferenceCFG
```

---

## Usage

Expand the **"DifferenceCFG"** accordion in the Script panel.

| Control | Default | Range | Description |
|---|---|---|---|
| **Enable DifferenceCFG** | Off | — | Master toggle. |
| **Reference CFG** | 5.0 | 0.0 – 20.0 (step 0.5) | The CFG scale that high-agreement elements are pulled toward. |
| **Difference Method** | `linear_distance` | dropdown | Selects the weighting curve (or the global fallback for `absolute_sum`). |
| **End At Percentage** | 0.80 | 0.0 – 1.0 | Step fraction after which the effect is disabled (percentage of the sampling schedule, converted to sigma internally). |

### Choosing a method

Ordered by how aggressively they pull agreeing elements toward Reference CFG:

`root_distance` ≥ `linear_distance` ≥ `squared_distance`

`root_distance` widens the effect to more of the image (small weights get boosted), `squared_distance` narrows it (small weights get suppressed further), and `linear_distance` sits in between. `absolute_sum` skips per-element selection entirely and applies one global correction based on the overall L1 norm ratio between cond and uncond — a coarser, cheaper effect.

---

## Recommended usage

Priority order in the pipeline: **TCFG (13.0) → SkimmedCFG (14.0) → DifferenceCFG (14.2) → CFGZeroStar (15.0) → CFG → MaHiRo (15.5)**.

DifferenceCFG runs after SkimmedCFG, so it corrects the already-skimmed output rather than the raw CFG output. This is a deliberate design choice: SkimmedCFG targets disagreement, DifferenceCFG targets agreement, so applying both is not redundant, but stacking many CFG-axis extensions at once at high CFG (25–30) can still accumulate error faster than any single extension anticipates. If results look over-corrected, try disabling one extension at a time to isolate the cause before adjusting values further.

---

## Background

Upstream's node family in `Extraltodeus/Skimmed_CFG` shares one Python file (`skimmed_CFG.py`) across several node classes, but Difference CFG's node class (`DifferenceCFG_PreCFG`) does not use the `Skim`-prefixed naming of the other nodes (`CFG_Skimming_...`, `SkimReplace...`, `SkimmedCFGLinInterp...`), and its algorithm does not call the shared masking functions those nodes use. Splitting it out keeps `sd-webui-SkimmedCFG` scoped to the actual skimming family, and keeps this repository's one-technique-per-repository structure consistent with the rest of the suite (`sd-webui-TCFG`, `sd-webui-MaHiRo`, `sd-webui-CFGZeroStar`, `sd-webui-NAGuidance`, `sd-webui-FreSca`, `sd-webui-DifferentialDiffusion`).

---

## Compatibility

| Environment | Status |
|---|---|
| reForge + SDXL | ✅ Confirmed |
| Forge Neo + SDXL | ✅ Confirmed |
| A1111 (no Forge backend) | ❌ Not supported |

---

## PNG infotext

The following parameters are embedded in generated PNG metadata (prefixed `DiffCFG`, independent of the old `Skimmed CFG Diff Method` / `Skimmed CFG Reference` keys from when this mode lived inside SkimmedCFG):

```
DiffCFG Reference, DiffCFG Method, DiffCFG End At
```

There is no dedicated enabled key; the presence of `DiffCFG Method` in a pasted infotext means the extension was on, its absence means off.

---

## File structure

```
sd-webui-DifferenceCFG/
├── scripts/
│   └── sd_webui_difference_cfg.py   # Script registration + UI
└── sd_webui_difference_cfg/
    ├── __init__.py                  # Re-exports from core
    └── core.py                      # Difference CFG algorithm
```

---

## Credits

- Algorithm: [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) — "Skimmed CFG - Difference CFG" node.

## License

This project ports the Difference CFG algorithm from [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG). Refer to the upstream repository for the original algorithm's licensing terms. No separate `LICENSE` file is included in this repository at this time.

---

# 日本語

**[English](#sd-webui-differencecfg)** | 日本語

Forge系WebUI（reForge / Forge Classic / Forge Neo）向けの、マスクを使わない大局的CFG再調整拡張機能です。

[Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) の **Difference CFG** ノードを移植したものです。チェックボックスを入れて生成するだけで使えます。

> 本拡張は元々`sd-webui-SkimmedCFG`内の1モードでした。SkimmedCFGのマスク方式（`get_skimming_mask`/`skimmed_CFG`）を一切使わない別系統のアルゴリズムであるため、独立したリポジトリとして切り出されています。詳細は[背景](#背景)を参照して下さい。

---

## 機能概要

通常のCFGは高スケール域で暴走しやすくなります。cond/uncondの予測がすでに一致している要素も、対立している要素と同じ強さで押し出されてしまうことが、高CFGでの色飽和や破綻の一因です。

Difference CFGは、通常のCFG出力を、より低い**Reference CFG**で得られたであろう出力へと再ブレンドします。ブレンドの強さは、cond/uncondの一致度合いに応じて要素ごとに決まります。一致度の高い要素ほど、穏やかなreference-scale側の結果に強く引き戻され、対立度の高い要素は通常CFGの出力に近いまま残ります。`absolute_sum`のみ要素ごとの選別を行わず、画像全体のL1ノルム比から単一のブレンド重みを算出します。

これはSkimmedCFG自体のマスクモード（Single Scale / Replace / Linear Interpolation / Dual Scales）が狙う**対立要素**とは正反対の選別対象です。両拡張は併用可能です。[推奨運用](#推奨運用)を参照して下さい。

---

## 特徴

- Scriptアコーディオンに **"DifferenceCFG"** として登録されます。
- 原典から忠実に移植した4手法:
  - `linear_distance`
  - `squared_distance`
  - `root_distance`
  - `absolute_sum`
- **Reference CFG** スライダーは原典デフォルトの0〜10から**0〜20**へ拡張。CFG 25〜30の高CFG運用でも、参照先の目標値自体を通常より高く設定できます。
- **End At Percentage** ゲート: 指定した進行割合以降は効果を無効化し、終盤のディテールに影響しないようにします。
- バックエンド適応フック: reForge / Forge ClassicはPre-CFG、Forge NeoはPost-CFG（TCFGを前段に併用している場合はそのスタッシュ済みdamped uncondを読み取り、未併用時は生のuncondにフォールバック）。
- PNG infotextによる設定の完全な往復（txt2img/img2imgへの送信で全設定を復元）に対応。
- フェイルセーフ: フック内部で例外が発生した場合、生成全体を止めずに標準CFG出力をそのまま返します。

---

## インストール

**Extensions → Install from URL:**

```
https://github.com/seti9585/sd-webui-DifferenceCFG
```

---

## 使い方

Scriptパネルの **"DifferenceCFG"** アコーディオンを展開します。

| コントロール | デフォルト | 範囲 | 説明 |
|---|---|---|---|
| **Enable DifferenceCFG** | オフ | — | 有効化トグル。 |
| **Reference CFG** | 5.0 | 0.0〜20.0（step 0.5） | 一致度の高い要素が引き戻される先のCFGスケール。 |
| **Difference Method** | `linear_distance` | ドロップダウン | 重み付け曲線を選択（`absolute_sum`はグローバル方式のfallback）。 |
| **End At Percentage** | 0.80 | 0.0〜1.0 | この進行割合以降は効果を無効化（内部でsigmaへ変換）。 |

### 手法の選び方

一致要素をReference CFG側へ引き戻す強さの順:

`root_distance` ≥ `linear_distance` ≥ `squared_distance`

`root_distance`は小さい重みを底上げして広く効き、`squared_distance`は小さい重みをさらに縮めて一部にしか効きません。`linear_distance`はその中間です。`absolute_sum`は要素ごとの選別を行わず、cond/uncond間の全体的なL1ノルム比から単一のグローバル補正を適用する、より粗くて軽い効果です。

---

## 推奨運用

パイプライン内の優先順位: **TCFG (13.0) → SkimmedCFG (14.0) → DifferenceCFG (14.2) → CFGZeroStar (15.0) → CFG → MaHiRo (15.5)**。

DifferenceCFGはSkimmedCFGの後段で動作するため、生のCFG出力ではなく、すでにskimmed済みの出力を補正します。これは意図的な設計です。SkimmedCFGは対立要素を、DifferenceCFGは一致要素を狙うため、両者の併用は冗長ではありません。ただし、高CFG（25〜30）でCFG軸の拡張機能を多数併用すると、単体の拡張機能が想定するより速く誤差が蓄積することがあります。過補正に見える場合は、値を調整する前に1つずつ無効化して原因を切り分けて下さい。

---

## 背景

`Extraltodeus/Skimmed_CFG`の原典では複数のノードクラスが1つのPythonファイル（`skimmed_CFG.py`）にまとまっていますが、Difference CFGのノードクラス（`DifferenceCFG_PreCFG`）だけは、他のノードが持つ`Skim`接頭辞の命名（`CFG_Skimming_...`、`SkimReplace...`、`SkimmedCFGLinInterp...`）から外れており、アルゴリズム自体もそれらノードが使う共有マスク関数を一切呼び出していません。独立させることで、`sd-webui-SkimmedCFG`を本来のskimming系統に絞り込みつつ、本拡張群の「1手法1リポジトリ」原則（`sd-webui-TCFG`、`sd-webui-MaHiRo`、`sd-webui-CFGZeroStar`、`sd-webui-NAGuidance`、`sd-webui-FreSca`、`sd-webui-DifferentialDiffusion`と同様）を保っています。

---

## 動作確認環境

| 環境 | 状態 |
|---|---|
| reForge + SDXL | ✅ 確認済み |
| Forge Neo + SDXL | ✅ 確認済み |
| A1111（Forgeバックエンドなし） | ❌ 非対応 |

---

## PNG infotext

以下のパラメータが生成PNGのメタデータに記録されます（`DiffCFG`接頭辞。本モードがSkimmedCFG内にあった当時の`Skimmed CFG Diff Method`/`Skimmed CFG Reference`キーとは独立しています）:

```
DiffCFG Reference, DiffCFG Method, DiffCFG End At
```

専用の有効化キーは持たず、ペーストされたinfotextに`DiffCFG Method`が存在すれば有効、存在しなければ無効として扱われます。

---

## ファイル構成

```
sd-webui-DifferenceCFG/
├── scripts/
│   └── sd_webui_difference_cfg.py   # Script登録 + UI
└── sd_webui_difference_cfg/
    ├── __init__.py                  # coreからの再エクスポート
    └── core.py                      # Difference CFGアルゴリズム本体
```

---

## クレジット

- アルゴリズム: [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) — "Skimmed CFG - Difference CFG" ノード。

## ライセンス

本プロジェクトは [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) のDifference CFGアルゴリズムを移植したものです。元アルゴリズムのライセンス条件については原典リポジトリを参照して下さい。本リポジトリには現時点で個別の`LICENSE`ファイルを同梱していません。

# sd-webui-DifferenceCFG

**EN** | [日本語](#日本語)

A mask-free, reference-scale-based global CFG re-adjustment for Forge-derived Stable Diffusion WebUIs (reForge / Forge Classic / Forge Neo).

Ported from the **Difference CFG** node in [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG). Tick a checkbox and generate — no workflow changes required.

> This extension was originally one of the modes inside `sd-webui-SkimmedCFG`. It has been split out into its own repository because it does not use SkimmedCFG's masking machinery (`get_skimming_mask` / `skimmed_CFG`) at all — it is a different family of algorithm that happens to share the same upstream author and repository. See [Background](#background) below.

---

## What it does

Standard CFG can "run away" at high scales: elements where the conditional and unconditional predictions already agree get pushed just as hard as elements where they disagree, which is part of what causes burn and oversaturation at high CFG.

Difference CFG re-blends the standard CFG output toward the output you *would* have gotten at a different **Reference CFG**, with the blend strength driven per-element by how strongly cond and uncond already agree. Elements with high agreement are pulled toward the reference-scale result; elements with strong disagreement are left closer to the session-scale output. `absolute_sum` instead computes a single global blend weight for the whole image rather than a per-element one.

Measured against native CFG changes on a fixed seed, the effect is what the name suggests: **Reference CFG re-targets the effective guidance scale while the CFG slider itself stays put**. See [Measured behaviour](#measured-behaviour) for the figures.

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
- **Bidirectional**: setting Reference CFG above the session CFG raises the effective scale rather than lowering it. Setting it equal to the session CFG is an exact no-op for three of the four methods.
- **End At Percentage** gate: disables the effect after the given fraction of steps.
- Priority insertion into the pre-CFG chain at **14.2**, so the hook order does not depend on which extensions happen to be installed.
- Backend-adaptive hook: Pre-CFG on reForge / Forge Classic, Post-CFG on Forge Neo (reads TCFG's stashed damped uncond when TCFG is enabled and stacked before it; falls back to the raw uncond otherwise).
- Full PNG infotext round-trip (send-to-txt2img / send-to-img2img preserves all settings), plus XYZ Grid axes for the enabled flag and the method dropdown.
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
| **Enable Difference CFG** | Off | — | Master toggle. |
| **Reference CFG** | 5.0 | 0.0 – 20.0 (step 0.5) | The CFG scale that high-agreement elements are pulled toward. Below the session CFG this calms the image; above it, it strengthens. Equal to the session CFG it does nothing for `linear_distance`, `squared_distance` and `root_distance`; `absolute_sum` is the exception and still shifts the image slightly — see [Measured behaviour](#measured-behaviour). |
| **Difference Method** | `linear_distance` | dropdown | Selects the weighting curve (or the global fallback for `absolute_sum`). |
| **End At Percentage** | 0.80 | 0.0 – 1.0 (step 0.01) | Step fraction after which the effect is disabled. A fine adjustment only — see [Measured behaviour](#measured-behaviour). |

### Choosing a method

Ordered by how far they move the effective scale toward Reference CFG, measured on a fixed seed:

`squared_distance` > `linear_distance` > `root_distance` > `absolute_sum`

`squared_distance` tracks a native CFG change most closely — set Reference CFG to 5 at a session CFG of 7 and the result lands at an effective scale of about 4.98. `linear_distance` is more conservative, reaching roughly 85 % of the nominal target, which is useful when you want something gentler than simply moving the CFG slider. `absolute_sum` is the weakest by a wide margin and its usable travel is roughly half that of the others.

`root_distance` is not simply a weaker setting. Above the session CFG it raises brightness and structure to the same degree as a native CFG increase while holding highlight clipping down — at Reference 9.0 with a session CFG of 7, brightness matched native CFG 9 but the clipped-pixel rate stayed at 10.0 % against native CFG 9's 12.4 %. Reach for it when you want the firmness of a higher CFG without the blown highlights.

---

## Measured behaviour

All figures below come from a 24-cut fixed-seed sweep on reForge: amanatsuIllustrious_v11, TDE Sampler `kutta4`, Align Your Steps, 35 steps, 896×1152, CFG 7, TCFG and DifferenceCFG only, with native CFG 3 / 5 / 7 / 9 generated as anchors. Metrics are taken on raw RGB pixel arrays.

### Reference CFG is an exact no-op at the session CFG

With Reference CFG set to the session CFG scale, `linear_distance`, `squared_distance` and `root_distance` all produced a **SHA-256 match** against the same generation with the extension disabled — a mean absolute difference of exactly zero across every pixel.

**`absolute_sum` is the exception.** It computes one global weight from an L1 norm ratio rather than per-element weights, so that ratio does not land exactly on 1 and a residual remains: 93.7 % of pixels differed, with a mean absolute RGB difference of 3.60. Visually this is indistinguishable from the disabled state, but it means `absolute_sum` cannot be used as a bitwise A/B reference.

### Reference CFG maps to effective CFG

Interpolated against the native CFG anchors, using `linear_distance`:

| Reference CFG | Effective CFG |
|---|---|
| 1.0 | 2.85 |
| 3.0 | 3.66 |
| 5.0 | 5.40 |
| 7.0 | 7.00 |
| 9.0 | 8.73 |
| 11.0 | 10.22 |

Two things to note. The slider is **compressed at the low end** — the nominal gap between 1.0 and 3.0 is worth only 0.81 of effective scale, and `linear_distance` cannot drive below roughly 2.8 however far you pull it down. And the mapping is **not one-to-one**: `linear_distance` reaches about 85 % of the nominal target, erring toward the session CFG.

By method, at a session CFG of 7:

| Method | Reference 5.0 → effective | Reference 9.0 → effective |
|---|---|---|
| `squared_distance` | 4.98 | 9.12 |
| `linear_distance` | 5.40 | 8.73 |
| `root_distance` | 5.89 | 8.96 |
| `absolute_sum` | 6.69 | 7.83 |

### The axis is continuous, not a set of discrete states

Across the Reference CFG sweep, mean brightness, brightness deviation, saturation, gradient magnitude and clipped-pixel rate were all monotonic at every scan point, and the pairwise difference matrix formed a monotonic chain — every cut's nearest neighbour was its neighbour on the slider. There is no threshold at which the image jumps to an unrelated result.

### End At Percentage is a fine adjustment

Lowering End At from 1.0 all the way to 0.5 moved the effective CFG only from 5.40 to 5.81, and changed the total difference from the disabled state by 1.1 %. That is two orders of magnitude smaller than the Reference CFG axis.

The reason is structural: late in sampling, sigma is small and the gap between the cond and reference predictions has already collapsed, so there is very little intervention left for the gate to switch off. **Use Reference CFG to change the image; use End At only for a final nudge.** The default of 0.80 is worth about 0.07 of effective CFG and is fine to leave alone.

---

## Recommended usage

Priority order in the pipeline:

**TCFG (13.0) → SkimmedCFG (14.0) → DifferenceCFG (14.2) → APG (14.5) → CFG → CFGZeroStar (15.0) → FreSca (15.2) → MaHiRo (15.5) → CFGNorm (16.0) → CFGRegulator (16.5)**

DifferenceCFG runs after SkimmedCFG, so it corrects the already-skimmed output rather than the raw CFG output. This is a deliberate design choice: SkimmedCFG targets disagreement, DifferenceCFG targets agreement, so applying both is not redundant. Stacking many CFG-axis extensions at high CFG (25–30) can still accumulate error faster than any single extension anticipates; if results look over-corrected, disable one extension at a time to isolate the cause before adjusting values further.

Note that the figures in [Measured behaviour](#measured-behaviour) were taken with only TCFG and DifferenceCFG active. SkimmedCFG also rewrites the unconditional prediction, and the interaction of two such rewrites in the same chain has not been measured. Treat the effective-CFG table as a guide for light stacks, not a guarantee inside a full chain.

### Chain ordering and the debug dump

Forge-based backends append pre-CFG hooks in registration order, which is effectively alphabetical and shifts with whatever else is installed. This extension inserts itself at priority 14.2 instead. Note that this is separate from `sorting_priority`, which controls only the accordion's position in the UI.

Set `SD_WEBUI_SETI_DEBUG=1` before launching to have the assembled chain printed at sampling time:

```
[DifferenceCFG] pre-CFG chain: _tcfg_pre_cfg_fn(13.0) -> _differencecfg_pre_cfg_fn(14.2)
```

If a hook you expected is missing, that extension is not enabled or failed to register. If the order differs from the list above, something in the chain is not participating in priority insertion.

---

## Background

Upstream's node family in `Extraltodeus/Skimmed_CFG` shares one Python file (`skimmed_CFG.py`) across several node classes, but Difference CFG's node class (`DifferenceCFG_PreCFG`) does not use the `Skim`-prefixed naming of the other nodes (`CFG_Skimming_...`, `SkimReplace...`, `SkimmedCFGLinInterp...`), and its algorithm does not call the shared masking functions those nodes use. Splitting it out keeps `sd-webui-SkimmedCFG` scoped to the actual skimming family, and keeps this repository's one-technique-per-repository structure consistent with the rest of the suite.

The measured behaviour bears this separation out. SkimmedCFG has no setting at which it becomes a no-op; DifferenceCFG has an exact one. Inheriting an assumption from one to the other would have been a mistake.

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

Two XYZ Grid axes are registered:

```
(Difference CFG) Enabled     True / False
(Difference CFG) Method      linear_distance / squared_distance / root_distance / absolute_sum
```

---

## File structure

```
sd-webui-DifferenceCFG/
├── LICENSE
├── NOTICE
├── scripts/
│   └── sd_webui_difference_cfg.py   # Script registration + UI
└── sd_webui_difference_cfg/
    ├── __init__.py                  # Re-exports from core
    └── core.py                      # Difference CFG algorithm
```

---

# 日本語

**[English](#sd-webui-differencecfg)** | 日本語

Forge 系 Stable Diffusion WebUI（reForge / Forge Classic / Forge Neo）向けの、マスクを使わない参照スケール方式の大局的 CFG 再調整拡張機能です。

[Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) の **Difference CFG** ノードからの移植です。チェックボックスを入れて生成するだけで、ワークフローの変更は必要ありません。

> 本拡張機能はもともと `sd-webui-SkimmedCFG` 内の 1 モードでした。SkimmedCFG のマスク機構（`get_skimming_mask` / `skimmed_CFG`）を一切使用しておらず、上流の作者とリポジトリが同じというだけで別系統のアルゴリズムであるため、独立したリポジトリに分離しています。詳細は[背景](#背景)を参照してください。

---

## 機能概要

標準的な CFG は高いスケールで暴走することがあります。条件付き予測と無条件予測がすでに一致している要素も、食い違っている要素と同じ強さで押し出されてしまうためで、これが高 CFG での焼き付きや過飽和の一因です。

Difference CFG は、標準 CFG の出力を「異なる **Reference CFG** で生成したならこうなったであろう出力」の方へ再ブレンドします。ブレンドの強さは、cond と uncond がどれだけ一致しているかによって要素ごとに決まります。一致度の高い要素は参照スケール側へ引き寄せられ、食い違いの強い要素はセッションスケールの出力に近いまま残されます。`absolute_sum` のみ、要素ごとではなく画像全体で単一のブレンド重みを算出します。

固定シードで素の CFG 変更と比較した実測では、名称のとおりの挙動が確認されています。すなわち **Reference CFG は、CFG スライダを動かさないまま実効的なガイダンススケールを付け替えます**。数値は[実測された挙動](#実測された挙動)を参照してください。

これは SkimmedCFG 自体のマスクモード（Single Scale / Replace / Linear Interpolation / Dual Scales）とは逆の選択対象です。あちらは*食い違い*を対象とします。両者は併用可能です。[推奨運用](#推奨運用)を参照してください。

---

## 特徴

- Script アコーディオンに **"DifferenceCFG"** として登録されます。
- 上流から忠実に移植した 4 つの差分手法:
  - `linear_distance`
  - `squared_distance`
  - `root_distance`
  - `absolute_sum`
- **Reference CFG** スライダ。上流の既定 0〜10 から **0〜20** に拡張しています。高 CFG 運用（CFG 25〜30）では参照先自体を通常の 5〜10 より大きく取る必要があるためです。
- **双方向**。Reference CFG をセッション CFG より高く設定すると、実効スケールは下がるのではなく上がります。セッション CFG と等しくすると、4 手法のうち 3 つで厳密に無効化されます。
- **End At Percentage** ゲート。指定した割合のステップ以降で効果を停止します。
- pre-CFG チェーンへの優先度 **14.2** での挿入。どの拡張機能がインストールされているかによってフック順序が変動しません。
- バックエンド適応型フック。reForge / Forge Classic では Pre-CFG、Forge Neo では Post-CFG。TCFG が有効かつ手前に積まれている場合は TCFG が退避した減衰済み uncond を読み、そうでなければ生の uncond にフォールバックします。
- PNG infotext の完全な往復（send-to-txt2img / send-to-img2img ですべての設定が保持されます）。有効フラグと手法ドロップダウンの XYZ Grid 軸も追加されます。
- フェイルセーフ。フック内部で例外が発生した場合、生成を壊さずに標準 CFG の出力をそのまま返します。

---

## インストール

**Extensions → Install from URL:**

```
https://github.com/seti9585/sd-webui-DifferenceCFG
```

---

## 使い方

Script パネルの **"DifferenceCFG"** アコーディオンを展開します。

| 項目 | 既定値 | 範囲 | 説明 |
|---|---|---|---|
| **Enable Difference CFG** | Off | — | マスタートグル。 |
| **Reference CFG** | 5.0 | 0.0 – 20.0（刻み 0.5） | 一致度の高い要素が引き寄せられる CFG スケール。セッション CFG より低ければ沈静化、高ければ強調。`linear_distance`・`squared_distance`・`root_distance` では等しければ何も起きませんが、`absolute_sum` は例外でわずかに変化します。[実測された挙動](#実測された挙動)を参照してください。 |
| **Difference Method** | `linear_distance` | ドロップダウン | 重み付けカーブ（`absolute_sum` の場合は大局的なフォールバック）を選択します。 |
| **End At Percentage** | 0.80 | 0.0 – 1.0（刻み 0.01） | この割合のステップ以降で効果を停止します。微調整専用です。[実測された挙動](#実測された挙動)を参照してください。 |

### 手法の選び方

固定シードでの実測にもとづく、実効スケールを Reference CFG へ動かす強さの順です。

`squared_distance` > `linear_distance` > `root_distance` > `absolute_sum`

`squared_distance` が素の CFG 変更を最も忠実に再現します。セッション CFG 7 で Reference CFG を 5 にすると、実効スケールは約 4.98 に着地します。`linear_distance` はより保守的で、名目値のおよそ 85 % までしか到達しません。CFG スライダを動かすより穏やかな効果が欲しい場合に有用です。`absolute_sum` は明確に最弱で、可動域は他の手法のおよそ半分です。

`root_distance` は単に弱い設定ではありません。セッション CFG より上側では、素の CFG 上昇と同程度に輝度と構造を引き上げつつ、白飛びだけを抑えます。セッション CFG 7 で Reference 9.0 とした実測では、輝度は素の CFG 9 と同等でありながら、白飛び画素率は素の CFG 9 の 12.4 % に対し 10.0 % に留まりました。高い CFG の締まりが欲しいが白飛びは避けたい場合に選んでください。

---

## 実測された挙動

以下の数値はすべて、reForge 上での固定シード 24 カット走査によるものです。amanatsuIllustrious_v11、TDE Sampler `kutta4`、Align Your Steps、35 ステップ、896×1152、CFG 7、有効な拡張機能は TCFG と DifferenceCFG のみ、加えて素の CFG 3 / 5 / 7 / 9 をアンカーとして生成しています。測定は RGB 生画素配列に対して行っています。

### Reference CFG はセッション CFG で厳密に無効化される

Reference CFG をセッションの CFG スケールと同じ値に設定したとき、`linear_distance`・`squared_distance`・`root_distance` の 3 つは、拡張機能を無効にした同一生成と **SHA-256 が完全に一致**しました。全画素で平均絶対差がちょうど 0 です。

**`absolute_sum` は例外です。** 要素ごとの重みではなく L1 ノルム比から単一の大局的な重みを算出するため、この比が厳密に 1 にならず残差が生じます。93.7 % の画素が変化し、平均絶対 RGB 差は 3.60 でした。見た目には無効時と区別できませんが、`absolute_sum` はビット単位の A/B 基準としては使えないということです。

### Reference CFG と実効 CFG の対応

素の CFG アンカーに対して内挿した、`linear_distance` での対応です。

| Reference CFG | 実効 CFG |
|---|---|
| 1.0 | 2.85 |
| 3.0 | 3.66 |
| 5.0 | 5.40 |
| 7.0 | 7.00 |
| 9.0 | 8.73 |
| 11.0 | 10.22 |

注意点が 2 つあります。スライダは**低域で圧縮されています**。1.0 と 3.0 の名目上の差は実効スケールにして 0.81 しかなく、`linear_distance` ではどれだけ下げても実効 CFG およそ 2.8 より下へは駆動できません。またこの対応は**一対一ではありません**。`linear_distance` は名目値のおよそ 85 % に到達し、セッション CFG 寄りにずれます。

セッション CFG 7 における手法別の対応です。

| 手法 | Reference 5.0 → 実効 | Reference 9.0 → 実効 |
|---|---|---|
| `squared_distance` | 4.98 | 9.12 |
| `linear_distance` | 5.40 | 8.73 |
| `root_distance` | 5.89 | 8.96 |
| `absolute_sum` | 6.69 | 7.83 |

### この軸は連続であり、離散的な状態の集合ではない

Reference CFG の走査全体で、平均輝度・輝度標準偏差・平均彩度・勾配強度・白飛び画素率のすべてが全走査点で単調でした。相互差分行列も単調な鎖を形成しており、どのカットも最近傍はスライダ上の隣接点です。ある閾値を境に無関係な結果へ飛ぶ、という挙動はありません。

### End At Percentage は微調整である

End At を 1.0 から 0.5 まで下げても、実効 CFG は 5.40 から 5.81 へ動くだけでした。無効時との総差分の変化は 1.1 % です。Reference CFG 軸と比べて 2 桁小さい効果量です。

理由は構造的なものです。サンプリング後半では σ が小さく、cond と参照予測の差はすでに縮小しているため、ゲートで停止するべき介入がほとんど残っていません。**絵を変えたいときは Reference CFG を、最後の微調整にのみ End At を使ってください。** 既定の 0.80 は実効 CFG にしておよそ 0.07 相当であり、そのままで問題ありません。

---

## 推奨運用

パイプライン内の優先度順:

**TCFG (13.0) → SkimmedCFG (14.0) → DifferenceCFG (14.2) → APG (14.5) → CFG → CFGZeroStar (15.0) → FreSca (15.2) → MaHiRo (15.5) → CFGNorm (16.0) → CFGRegulator (16.5)**

DifferenceCFG は SkimmedCFG の後に実行されるため、生の CFG 出力ではなく、すでに skim 処理された出力を補正します。これは意図的な設計です。SkimmedCFG は食い違いを、DifferenceCFG は一致を対象とするため、両方を適用することは冗長ではありません。ただし高 CFG（25〜30）で CFG 軸の拡張機能を多数積み重ねると、個々の拡張機能が想定する以上の速さで誤差が蓄積することがあります。過補正に見える場合は、値をさらに調整する前に 1 つずつ無効化して原因を切り分けてください。

なお[実測された挙動](#実測された挙動)の数値は、TCFG と DifferenceCFG のみを有効にして取得したものです。SkimmedCFG も無条件予測を書き換えるため、同一チェーン内で 2 つの書き換えが相互作用した場合の挙動は未測定です。実効 CFG の対応表は軽いスタックでの目安であり、フルチェーン内での保証ではないものとして扱ってください。

### チェーン順序とデバッグダンプ

Forge 系バックエンドは pre-CFG フックを登録順に追加します。これは実質的にアルファベット順であり、他に何がインストールされているかによって変動します。本拡張機能は代わりに優先度 14.2 の位置へ自身を挿入します。これは `sorting_priority` とは別物である点に注意してください。`sorting_priority` は UI 上のアコーディオンの位置のみを制御します。

起動前に `SD_WEBUI_SETI_DEBUG=1` を設定すると、サンプリング時に組み立てられたチェーンが出力されます。

```
[DifferenceCFG] pre-CFG chain: _tcfg_pre_cfg_fn(13.0) -> _differencecfg_pre_cfg_fn(14.2)
```

想定していたフックが現れない場合、その拡張機能が有効化されていないか、登録に失敗しています。順序が上記の一覧と異なる場合、チェーン内のいずれかが優先度挿入に参加していません。

---

## 背景

上流 `Extraltodeus/Skimmed_CFG` のノード群は 1 つの Python ファイル（`skimmed_CFG.py`）を複数のノードクラスで共有していますが、Difference CFG のノードクラス（`DifferenceCFG_PreCFG`）は他のノード（`CFG_Skimming_...`、`SkimReplace...`、`SkimmedCFGLinInterp...`）のような `Skim` 接頭辞の命名を用いておらず、そのアルゴリズムは他ノードが使う共有マスク関数を呼び出しません。分離することで `sd-webui-SkimmedCFG` を本来の skimming 系統に限定でき、本スイート全体で貫いている「1 技術 1 リポジトリ」の構成とも整合します。

実測された挙動もこの分離を裏付けています。SkimmedCFG には無効化される設定が存在しませんが、DifferenceCFG には厳密なそれが存在します。一方の前提を他方へ引き継いでいたら誤りになっていました。

---

## 動作確認環境

| 環境 | 状態 |
|---|---|
| reForge + SDXL | ✅ 確認済み |
| Forge Neo + SDXL | ✅ 確認済み |
| A1111（Forge バックエンドなし） | ❌ 非対応 |

---

## PNG infotext

以下のパラメータが生成された PNG のメタデータに埋め込まれます（接頭辞 `DiffCFG`。本モードが SkimmedCFG 内にあった頃の `Skimmed CFG Diff Method` / `Skimmed CFG Reference` とは独立です）。

```
DiffCFG Reference, DiffCFG Method, DiffCFG End At
```

専用の有効化キーは持たず、ペーストされた infotext に `DiffCFG Method` が存在すれば有効、存在しなければ無効として扱われます。

XYZ Grid には次の 2 軸が登録されます。

```
(Difference CFG) Enabled     True / False
(Difference CFG) Method      linear_distance / squared_distance / root_distance / absolute_sum
```

---

## ファイル構成

```
sd-webui-DifferenceCFG/
├── LICENSE
├── NOTICE
├── scripts/
│   └── sd_webui_difference_cfg.py   # Script登録 + UI
└── sd_webui_difference_cfg/
    ├── __init__.py                  # coreからの再エクスポート
    └── core.py                      # Difference CFGアルゴリズム本体
```

---

## Acknowledgements / 謝辞

**Extraltodeus**

The Difference CFG algorithm is the work of [**Extraltodeus**](https://github.com/Extraltodeus), published in [Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) as the `DifferenceCFG_PreCFG` node. This extension exists only because that node exists.

Difference CFG のアルゴリズムは [**Extraltodeus**](https://github.com/Extraltodeus) 氏によるもので、[Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) の `DifferenceCFG_PreCFG` ノードとして公開されています。本拡張機能は同ノードの存在があってはじめて成立しています。

**Shiba-2-shiba**

Development of this whole extension suite started from the articles and Forge Classic implementation of [**Shiba-2-shiba**](https://note.com/gentle_murre488). Sincere thanks.

本拡張スイート全体の開発は、[**Shiba-2-shiba**](https://note.com/gentle_murre488) 氏の記事および Forge Classic 向け実装をきっかけに始まりました。深く感謝します。

---

## License / ライセンス

**Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright (c) 2026 seti9585

This extension is a port of the Difference CFG algorithm from [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG), which is licensed under the Apache License 2.0. `interpolated_scales()` and the body of the pre-CFG patch are ported without algorithmic change, so this is a derivative work and is distributed under the same licence. The modifications made in porting it — the WebUI script layer, the Forge Neo post-CFG path, the priority insertion, the extended Reference CFG range, the debug chain dump and the infotext and XYZ Grid handling — are recorded in `NOTICE` as the licence requires.

Earlier revisions of this file stated that no `LICENSE` file was included and referred readers to the upstream repository for terms. That was insufficient: Apache-2.0 requires the licence text and the change notice to travel with the derivative. This has been corrected.

本拡張機能は [Extraltodeus/Skimmed_CFG](https://github.com/Extraltodeus/Skimmed_CFG) の Difference CFG アルゴリズムを移植したものです。同リポジトリは Apache License 2.0 でライセンスされています。`interpolated_scales()` および pre-CFG パッチ本体はアルゴリズムを変更せずに移植しているため、本拡張機能は派生物であり、同一ライセンスで配布します。移植にあたって加えた変更（WebUI スクリプト層、Forge Neo 向け post-CFG 経路、優先度挿入、Reference CFG 範囲の拡張、デバッグ用チェーンダンプ、infotext および XYZ Grid の取り扱い）は、ライセンスの要求に従い `NOTICE` に記録しています。

本ファイルの以前の版は「個別の `LICENSE` ファイルを同梱していない」と記載し、ライセンス条件については上流リポジトリを参照するよう案内していました。これは不十分でした。Apache-2.0 は、ライセンス本文と変更告知が派生物とともに配布されることを要求します。以上により訂正しました。

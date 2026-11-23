> Historical record / 履歴資料：当時の実装・検証状況です。最新状況は [2026-11-17の検証記録](VALIDATION_2026-11-17.md) を参照してください。

# Reference CFG diagnostics / Reference CFG診断

## English
This update adds measurements only. The Auto solver, Budget meaning/default 0.10,
algorithm identifier, original DifferenceCFG functions and SkimmedCFG are unchanged.

Only AUTO_APPLIED contributes to Reference CFG min, running mean, max and count.
floor_hits counts applied calls whose reference matches that call's effective
Skimming CFG. floor_rate is floor_hits divided by count, expressed as a percentage.
The diagnostic-only equality tolerance is an absolute 0.000000001, with no
relative tolerance. It does not affect reference selection or correction.

AUTO_NO_CORRECTION, gate skips, fallback and invalid calls are excluded.
If count is zero, min/mean/max and floor_rate are N/A.
Counts represent denoiser hook calls, not images, sampler steps or tensor elements.

A fresh collector is created in process() for each generation request.
Re-registering hooks in the same request preserves the collector. A new request,
including one after interruption or switching to manual mode, discards old data
and removes old diagnostic metadata. No global collector or tensor is retained.

At postprocess_batch, the summary is written to DiffCFG Auto Reference Stats
in extra_generation_params, for the host's subsequent infotext creation and PNG
saving. In multi-batch requests each PNG contains the cumulative request statistics
through its completed batch, not statistics for later batches that have not run.
Console output at postprocess reports the final request-wide total.
Existing DiffCFG Auto Status is kept separately. Manual Reference CFG metadata
is not overwritten. The summary does not participate in PNG setting restoration.

The collector consumes Python scalar reference/skimming values already available
in the successful path. It adds no tensor operation, item(), tolist(), cpu(),
float(tensor), logging of tensors or GPU-to-CPU transfer. Forge Neo has no new
reference diagnostics.

## 日本語
今回の更新は診断機能だけです。Autoの計算式、Budgetの意味と初期値0.10、
アルゴリズム版、既存DifferenceCFG補正、SkimmedCFGは変更していません。

AUTO_APPLIEDだけから、Reference CFGの最小値・平均値・最大値・適用回数を集計します。
下限到達回数は、その回のSkimming CFGまでReference CFGが下がった回数です。
下限到達率の分母はAUTO_APPLIEDの回数です。一致の判定には絶対誤差
0.000000001以内を使用します。診断だけの判定で、実際の補正には影響しません。

補正ゼロ・適用区間外・手動フォールバック・エラーは統計から除外します。
適用ゼロ回なら最小・平均・最大・到達率はN/Aです。
回数は補正処理が呼ばれた回数であり、画像枚数やStepsとは限りません。

生成開始ごとに集計を作り直し、中断後や手動モードへの切り替えでも前回値を残しません。
同じ生成要求内の処理再登録では集計を維持します。
バッチ終了時に生成情報のDiffCFG Auto Reference Statsへ要約を書き、
生成終了時にはコンソールへ最終集計を表示します。
複数バッチの場合、PNGはそのバッチ終了までの累積値、コンソールは生成要求全体の値です。
すでに保存したPNGへ後続バッチの結果をさかのぼって書き直すことはしません。

すでにCPU側にあるReference CFGとSkimming CFGだけを使い、診断による
GPUからCPUへの同期は追加しません。手動Reference CFGの保存値は維持します。
Forge Neo向けの新しい診断対応は行っていません。

## Validation / 検証
38 CPU tests passed on Python 3.13 / PyTorch 2.14.1+cpu.
- 96 before/after Auto cases have identical tensors, status and actual reference.
- Original manual regression suites also pass (128 + 128 cases).
- Known min/mean/max/count and per-call floor percentages match expected values.
- Non-AUTO_APPLIED calls, zero applications, interrupted requests, repeated requests,
  manual switches and two-batch cumulative metadata snapshots are covered.
- Tensor conversion/transfer call counts match the preceding implementation:
  Budget 0: float=2, bool=2, to=12, tolist=1.
  Budget 0.10 and 1: float=2, bool=3, to=15, tolist=2.
  No item/cpu/numpy calls were observed in either run.
- The budget solver AST and constants match the preceding implementation.
- DifferenceCFG core.py and all SkimmedCFG files are verified byte-for-byte unchanged.

38項目のCPU試験に合格。実際のGPU同期を計測した結果ではなく、CPUテストでの
呼び出し回数比較と診断コードの構造確認です。
reForge実機での画像生成・PNGファイル保存は未実施です。
保存前の生成情報更新、生成終了後のコンソール表示は代用ホストで検証しました。
数値試験を実画像の画質検証と読み替えないでください。
詳細はDIAGNOSTICS_TEST_LOG.txtを参照してください。

For optional developer tests / 開発用の再試験だけ行う場合:
- Install: python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
- Run in the DifferenceCFG folder beside the paired SkimmedCFG folder:
  python -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v
通常利用に追加ライブラリの導入は不要です。

## License and sources / ライセンス・典拠
The existing LICENSE and NOTICE remain applicable.
The preceding local v2 source and tests are the regression baseline; the original
manual algorithm is derived from https://github.com/Extraltodeus/Skimmed_CFG .

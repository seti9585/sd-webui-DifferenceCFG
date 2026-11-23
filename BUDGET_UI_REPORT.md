> Historical record / 履歴資料：当時の実装・検証状況です。最新状況は [2026-11-17の検証記録](VALIDATION_2026-11-17.md) を参照してください。

# Auto Budget UI変更 — 2026-10-25

## 変更内容
通常のAuto Correction Budgetを次の値に変更しました。
- 最小値: 0.00
- 最大値: 0.10
- 刻み幅: 0.005
- UI初期値: 0.03

実行コードの変更はscripts/sd_webui_difference_cfg.pyだけです。
Auto計算式、補正、診断、Contract修復、Budget内部許容範囲0～1は変更していません。
手動Reference CFGとAuto OFFの補正動作も維持しています。

## 既存設定との互換性
0.10を超えるBudgetの旧PNGを復元した場合、その値を切り捨てず、
スライダーの上限をその値まで一時的に拡張します。
0.10以下の設定を復元するかUIを新しく開くと通常の上限へ戻ります。
旧API等で刻み0.005に一致しない値が保存されていた場合は、値を丸めず、
復元時だけ必要な細かい刻みを使います。

初期画面の0.03と、過去設定の省略値は区別しています。
旧PNGでBudget項目が欠ける場合と、旧API引数でBudgetを省略する場合は、
従来どおり0.10を維持します。Auto項目のない旧PNGはAuto OFFです。
正常な保存値は0～1の範囲でそのまま使用します。
壊れた値・非有限値・内部範囲外の値は警告し、UI初期値0.03に戻します。

do_not_save_to_config=Trueを維持しています。
利用者のui-config.jsonには直接アクセスせず、編集もしません。
公開reForgeのUiLoadsave.add_componentを分離実行し、保存済みの
value/minimum/maximum/stepがBudgetスライダーを上書きしないことを試験しました。
公開reForgeのpaste_funcも分離実行し、復元値と上限の更新が渡されることを確認しました。
実機ブラウザのPNG Info操作は今回未実施です。

## 暫定値の根拠（ユーザー報告）
reForge＋SDXL Illustrious、SkimmedCFG Linear Interpolation、Skimming CFG 3、
DifferenceCFG linear_distance、30ステップでの実機測定として提示された値です。
こちらで画像を再生成した測定結果ではありません。

| CFG | Budget | Reference平均 | 備考 |
| --- | --- | --- | --- |
| 20 | 0.00 | 補正なし | |
| 20 | 0.01 | 19.165 | |
| 20 | 0.03 | 17.508 | |
| 20 | 0.05 | 15.881 | |
| 20 | 0.10 | 11.101 | 構図・背景の変化が大きいとの評価 |
| 25 | 0.03 | 21.069 | AUTO_APPLIED=18、floor_hits=0 |

複数seedで0.02～0.03が好みに合うとのユーザー評価に基づき0.03を暫定採用。
画質上の普遍的な最適値を意味しません。

## 検証結果
57項目PASS（既存47項目＋UI/互換性10項目、今回はスキップなし）。
- txt2img/img2imgの初期値・最小・最大・刻み・設定保存除外を確認。
- 旧PNGの0.15/0.25/0.50/1.00を切り捨てず復元。
- 0.0375/0.123456/0.1001/0.00001も値を丸めず復元。
- 通常値への復元で上限0.10・通常刻みに戻ることを確認。
- 旧PNGのAuto OFF復元、手動Reference CFGとBudgetのinfotext往復を確認。
- 公開reForgeの復元処理とui-config適用処理を分離して試験。
- 既存の補正・統計・Contract修復テストはすべてPASS。
- Auto計算・診断・core.pyおよびSkimmedCFG全ファイルをSHA256で照合し不変を確認。

Python 3.13 / PyTorch 2.14.1+cpu。GPUでの新規生成やブラウザUI操作は未実施です。
既存試験の通常Auto96条件、手動128＋128条件、推論テンソル32条件の一致も維持。
ログはBUDGET_UI_TEST_LOG.txtに保存しました。

## 開発用の再試験
通常利用に追加インストールは不要です。
- 開発用ライブラリ: python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
- DifferenceCFGフォルダで: python -X utf8 -B -m unittest discover -s tests -p "test_*.py" -v

公開ホスト実装の2試験にはDIFFCFG_HOST_REFERENCE環境変数で、
infotext_utils.pyとui_loadsave.pyを保存したフォルダを指定します。
この指定がなければその2試験だけスキップします。
今回取得したファイルとSHA256は作業フォルダのhost-referenceに保存しています。

## 確認した一次ソース
- https://raw.githubusercontent.com/Panchovix/stable-diffusion-webui-reForge/main/modules/infotext_utils.py
- https://raw.githubusercontent.com/Panchovix/stable-diffusion-webui-reForge/main/modules/ui_loadsave.py
- https://raw.githubusercontent.com/gradio-app/gradio/gradio@3.41.2/gradio/components/slider.py

公開mainの取得時点のコードを参照しました。ローカルreForge本体はアクセス範囲外のため未読です。

# CV（statsbio.com/cv/）の更新方法

CV の Web版と PDF は `cv.yaml` と researchmap から自動で作られます。**生成物（index.html・PDF）は手で直さない**でください。

| 変えたいもの | やること |
|---|---|
| 論文が増えた | researchmap に登録するだけ（毎週月曜に自動反映） |
| researchmap に無い論文をすぐ載せたい | `cv.yaml` の `publications.extra` に追加 |
| 共同筆頭など役割の指定 | `cv.yaml` の `publications.roles`（DOI → 役割） |
| 職歴・助成・受賞・資格・プレプリント | `cv.yaml` の該当ブロックを編集 |
| researchmap 側の誤記 | `cv.yaml` の `publications.overrides` / `author_name_fixes` / `journal_names` |

- 反映: `cv.yaml` を main に push すると GitHub Actions（`.github/workflows/cv.yml`）が作り直して公開します。
- 更新日: 中身が変わったときだけ日付が変わります（トップページの「最終更新」も連動）。
- 手元で作る: `pip install -r cv/requirements.txt && python cv/build_cv.py`（`--fetch` で researchmap を再取得、Chrome が必要）
- 出力: `index.html`（Web版）/ `Kazuki_Nishida_CV.pdf`（完全版）/ `Kazuki_Nishida_CV_short.pdf`（短縮版）

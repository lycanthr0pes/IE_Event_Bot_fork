# イベント同期の再試行

Google→Notion/Discordの適用と、Discord→Notion/Googleの同期・新規作成通知・削除が対象。前日リマインドとQ&Aの定期ジョブ、一覧取得・認証の失敗は別処理であり、このイベント単位の上限には含めない。

## 回数と処理順

- `SYNC_EVENT_MAX_RETRIES=5`: 初回を含め最大6回。設定可能範囲は0〜20。
- `SYNC_EVENT_RETRY_SECONDS=300`: 失敗記録時刻から次回試行までの最小秒数。設定可能範囲は1〜86400。
- 範囲外・不正値は既定値へ戻す。0秒での即時再試行は許可しない。
- 失敗項目は未処理項目の後ろへ回す。待機中・隔離中の項目を飛ばして、処理可能な後続項目へ進む。
- 手動HTTP、Webhook、Cronで同じ保存済み期限を使う。呼出し回数ではなく、実際に処理して失敗した回数を数える。
- 同期から通知へ移る際も同じ未完了操作の回数を引き継ぐ。成功した操作はqueueから除去する。
- 再試行待ち・隔離が残る場合、結果の`ok`はfalse。Googleのカーソルと全体同期の最終成功時刻を進めない。他のイベントの処理は続ける。

通常の5分Cronで毎回処理できれば、初回失敗から約25分後に6回目へ達する。混雑・停止・ロック競合で遅れることがあり、25分以内の通知は保証しない。

## 保存形式と隔離

新しいKV bindingやD1は追加しない。`STATE_KV`の既存キーを使い、通常入口の`SYNC_COORDINATOR`による排他を保つ。

| キー | 内容 |
| --- | --- |
| `sync:google_apply_queue` | Googleイベント本体と`_sync_retry` |
| `sync:discord_notion_queue` | `id`・`op`・必要な`notification`と`_sync_retry` |
| `discord:snapshot` | `_pending_sync`内にもDiscordの未完了・隔離操作を保持 |

`_sync_retry`は失敗回数`attempts`、次回試行時刻`next_attempt_at`、隔離時刻`quarantined_at`、再投入世代`generation`、固定エラー分類`last_error`を持つ。時刻はUTCのUnix秒。例外本文やトークンは記録しない。旧queue項目は初回扱いで読み込む。

上限に達しても項目を削除しない。通常処理から除外し、管理APIでの再投入を待つ。隔離後はDiscord一覧から消えても記録を保持する。`pending_events` / `pending_changes`は隔離を除く残件数、`quarantined_events` / `quarantined_changes`は隔離件数。

Googleの同一イベントを再取得しても回数・期限・隔離を解除しない。予定の修正・キャンセル内容はqueueへ取り込む。Discordの明示再投入は世代を増やし、古いsnapshotとの照合で再隔離へ巻き戻さない。

## 確認と再投入

両APIは通常Workerの`INTERNAL_API_TOKEN`によるBearer認証を要求する。E2E Workerの許可routeには追加しない。

1. `GET /admin/sync/quarantine?source=discord`または`source=google`で隔離一覧を確認する。
2. 原因を修正する。
3. `POST /admin/sync/requeue`へ次のJSONを送る。

```json
{"source": "discord", "event_id": "対象イベントID"}
```

再投入は通常同期と同じDOロックで保護する。KV・ロックが利用できなければ503、同期中なら409。対象がなければ404、隔離対象でなければ409。再投入直後の同じ要求の再送は200・`requeued=false`で、回数を重ねてリセットしない。

再投入は外部APIへ送信せず、カウンターと期限をリセットしてqueue末尾へ戻す。次の通常同期で処理する。投稿済みのDiscordメッセージIDを保持し、イベントに新しい変更がなければリアクションのみを再試行する。対象が既に一覧から消えていれば、既存の削除・通知取消ルールに従う。再投入自体は通知の配信保証ではない。

## 検証境界

外部通信を遮断したテストで、後続処理、期限直前・期限到達、6回目の隔離、再投入、投稿済みID保持、同期ロック競合を確認する。既存の復旧テストは同期間の時計を5分進める。E2E所有環境のenv viewは再試行間隔を1秒に短縮し、保存済み期限を待って既存シナリオを実行する。この短縮E2Eは既定300秒の実時間検証ではない。

KV保存失敗、両方の状態が古い場合、外部API成功後の応答喪失では余分な試行・重複投稿があり得る。[Workers KVの結果整合性](https://developers.cloudflare.com/kv/concepts/how-kv-works/)を含む既存の制約は残り、厳密な最大送信回数や一度限りの配信は保証しない。

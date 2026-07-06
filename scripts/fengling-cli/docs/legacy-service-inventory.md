# Legacy Service Inventory

This project had two different network dependency groups.

## Removed / Legacy Fengling Server

These belonged to the old Fengling service and should not be required by the Mac CLI.

| Purpose | Endpoint / source | Evidence |
| --- | --- | --- |
| License activation | `https://www.fenglingmusic.com/license/api/activate` and fallback `http://152.136.143.207/license/api/activate` | `scripts/license_client.py`, `Studio_WPF_CN.ps1` |
| License verification | `https://www.fenglingmusic.com/license/api/verify` and fallback `http://152.136.143.207/license/api/verify` | `scripts/license_client.py`, `license_guard.py`, `Studio_WPF_CN.ps1` |
| Update check | `https://www.fenglingmusic.com/license/api/update` | `Studio_WPF_CN.ps1` only; Windows shell concern |
| Lyrics / prompt conversion | `https://www.fenglingmusic.com/license/api/lyrics/convert` and fallback `http://152.136.143.207/license/api/lyrics/convert` | `Studio_WPF_CN.ps1` only; not in the backend upload script |

The Mac CLI removes the local license/card-key code path from the migrated backend and does not call update or lyrics conversion endpoints.

## Still Required: Suno

The actual recut/upload/render workflow depends on Suno and the user's logged-in browser session.

| Purpose | Endpoint family |
| --- | --- |
| Login/token check | `https://suno.com`, Clerk pages, local Chrome CDP |
| Feed preflight and scanning | `https://studio-api-prod.suno.com/api/feed/...` |
| Upload init/status/clip init | `/api/uploads/audio/...` |
| Trash uploaded slices | `/api/gen/trash` |
| Metadata | `/api/gen/{clip_id}/set_metadata/` |
| Studio project read/save/create | `/api/studio/project/...`, `/api/studio/save-project`, `/api/studio/create-project...` |
| Render | `/api/studio/render-state`, `/api/feed/v3` |
| Final audio download | `audio_url` returned by Suno, often `cdn1.suno.ai` |

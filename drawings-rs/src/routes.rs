//! The drawings routes of `server.py` (`/api/drawings...`), filed under the engine's current source
//! and symbol. Each write resets the alert checker's cache (a moved drawing moves its alert).

use serde_json::{json, Map, Value};

use super::checker::DrawingAlerts;
use super::clean::{clean_alert, clean_drawing, drawing_payload};
use wednesday_engine::{Answer, Refusal};
use wednesday_scan::{unix, Bar};
use wednesday_store::Store;

/// An exception the Python route doesn't catch (a database error): FastAPI's 500.
fn internal(e: String) -> Refusal {
    log::error!("drawings: {e}");
    Refusal::new(500, "Internal Server Error")
}

fn changed(checker: Option<&DrawingAlerts>) {
    if let Some(c) = checker {
        c.invalidate();
    }
}

/// The alert's armed bar: the feed-clock unix time of the last closed M1 bar, None without bars.
pub fn armed_bar(m1: &[Bar]) -> Option<i64> {
    m1.last().map(|b| unix(b.time))
}

/// `GET /api/drawings`: the drawings and their alerts.
pub async fn list(store: &Store, source: &str, symbol: &str) -> Answer {
    let rows = store.drawings(source, symbol).await.map_err(internal)?;
    let alerts = store.drawing_alerts(source, symbol).await.map_err(internal)?;
    Ok(json!({"source": source, "symbol": symbol, "drawings": rows.iter().map(drawing_payload).collect::<Vec<_>>(),
              "alerts": alerts.iter().map(drawing_payload).collect::<Vec<_>>()}))
}

/// `GET /api/drawings/alerts`: just the alerts, polled after each scan so a fired one greys out.
pub async fn alerts(store: &Store, source: &str, symbol: &str) -> Answer {
    let alerts = store.drawing_alerts(source, symbol).await.map_err(internal)?;
    Ok(json!({"alerts": alerts.iter().map(drawing_payload).collect::<Vec<_>>()}))
}

/// `PUT /api/drawings/{id}/alert`: set (or re-arm) a drawing's alert; only closed bars after
/// `armed_bar` count.
pub async fn alert_put(store: &Store, source: &str, symbol: &str, drawing_id: &str, armed_bar: Option<i64>,
                       body: &Map<String, Value>, checker: Option<&DrawingAlerts>) -> Answer {
    let drawings = store.drawings(source, symbol).await.map_err(internal)?;
    let Some(drawing) = drawings.iter().find(|d| d.get("id").and_then(Value::as_str) == Some(drawing_id)) else {
        return Err(Refusal::new(404, "No such drawing: save it first"));
    };
    let kind = drawing.get("kind").and_then(Value::as_str).unwrap_or("");
    let cleaned = clean_alert(kind, body, armed_bar).map_err(|e| Refusal::new(422, e))?;
    let mut row = Map::new();
    row.insert("drawing_id".into(), drawing_id.into());
    row.insert("source".into(), source.into());
    row.insert("symbol".into(), symbol.into());
    row.extend(cleaned);
    store.drawing_alert_put(&row).await.map_err(internal)?;
    changed(checker);
    Ok(drawing_payload(&row))
}

/// `DELETE /api/drawings/{id}/alert`.
pub async fn alert_delete(store: &Store, source: &str, symbol: &str, drawing_id: &str,
                          checker: Option<&DrawingAlerts>) -> Answer {
    if !store.drawing_alert_delete(drawing_id, source, symbol).await.map_err(internal)? {
        return Err(Refusal::new(404, "That drawing has no alert"));
    }
    changed(checker);
    Ok(json!({"deleted": drawing_id}))
}

/// `PUT /api/drawings/{id}`: save a drawing; its alert (if any) stays and follows it.
pub async fn put(store: &Store, source: &str, symbol: &str, drawing_id: &str, body: &Map<String, Value>,
                 checker: Option<&DrawingAlerts>) -> Answer {
    let row = clean_drawing(drawing_id, body).map_err(|e| Refusal::new(422, e))?;
    let mut stored = row.clone();
    stored.insert("source".into(), source.into());
    stored.insert("symbol".into(), symbol.into());
    store.drawing_put(&stored).await.map_err(internal)?;
    changed(checker); // a moved drawing moves its alert
    Ok(Value::Object(row))
}

/// `DELETE /api/drawings/{id}`: the drawing only; its alert stays, for undo.
pub async fn delete(store: &Store, source: &str, symbol: &str, drawing_id: &str, checker: Option<&DrawingAlerts>) -> Answer {
    if !store.drawing_delete(drawing_id, source, symbol).await.map_err(internal)? {
        return Err(Refusal::new(404, "No such drawing"));
    }
    changed(checker);
    Ok(json!({"deleted": drawing_id}))
}

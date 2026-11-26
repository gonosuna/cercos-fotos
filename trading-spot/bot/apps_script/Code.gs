/**
 * Recibe los eventos del bot y los escribe en la planilla.
 * Hojas: "Operaciones" (una fila por compra/venta), "Diario" (una fila por dia, se reescribe si se repite) y "Mensual" (se arma sola).
 * Instalacion: ver README.md (Extensiones > Apps Script > pegar > Propiedades del script: SECRET > Implementar como app web).
 */
var OPS = ["Fecha","Hora","Par","Lado","Motivo","Precio","Cantidad","USDT","Comisión","Resultado USDT","Resultado %","Tramo","Modo","Decisión"];
var DIA = ["Fecha","Resultado del día","Acumulado realizado","Equity","% sobre capital","Invertido","Posiciones","Compras","Ventas"];

function sheet_(name, header) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name) || ss.insertSheet(name);
  if (sh.getLastRow() === 0) { sh.getRange("A:A").setNumberFormat("@"); sh.appendRow(header); sh.setFrozenRows(1); sh.getRange(1,1,1,header.length).setFontWeight("bold"); }
  return sh;
}

function setup() { sheet_("Operaciones", OPS); sheet_("Diario", DIA); sheet_("Mensual", ["Mes","Resultado realizado","Equity fin de mes","Compras","Ventas"]); }

function doPost(e) {
  var d = JSON.parse(e.postData.contents);
  if (d.secret !== PropertiesService.getScriptProperties().getProperty("SECRET")) {
    return ContentService.createTextOutput(JSON.stringify({ok: false, error: "secret"})).setMimeType(ContentService.MimeType.JSON);
  }
  var lock = LockService.getScriptLock(); lock.waitLock(20000);
  try {
    if (d.tipo === "operacion") {
      sheet_("Operaciones", OPS).appendRow([d.fecha,d.hora,d.par,d.lado,d.motivo,d.precio,d.cantidad,d.usdt,d.comision,d.pnl_usdt,d.pnl_pct,d.tramo,d.modo,d.decision]);
    } else if (d.tipo === "diario") {
      var sh = sheet_("Diario", DIA), fila = [d.fecha,d.resultado_dia,d.acumulado,d.equity,d.pct_capital,d.invertido,d.posiciones,d.compras,d.ventas];
      var last = sh.getLastRow(), found = 0;
      if (last > 1) { var fechas = sh.getRange(2,1,last-1,1).getDisplayValues(); for (var i=0;i<fechas.length;i++) if (fechas[i][0] === d.fecha) { found = i+2; break; } }
      if (found) sh.getRange(found,1,1,fila.length).setValues([fila]); else sh.appendRow(fila);
      mensual_();
    }
  } finally { lock.releaseLock(); }
  return ContentService.createTextOutput(JSON.stringify({ok: true})).setMimeType(ContentService.MimeType.JSON);
}

function mensual_() {
  var rows = sheet_("Diario", DIA).getDataRange().getDisplayValues().slice(1), m = {}, order = [];
  rows.forEach(function (r) {
    var k = r[0].substring(0,7); if (!m[k]) { m[k] = {res:0, eq:0, c:0, v:0}; order.push(k); }
    m[k].res += parseFloat(r[1])||0; m[k].eq = parseFloat(r[3])||0; m[k].c += parseInt(r[7])||0; m[k].v += parseInt(r[8])||0;
  });
  var sh = sheet_("Mensual", ["Mes","Resultado realizado","Equity fin de mes","Compras","Ventas"]);
  if (sh.getLastRow() > 1) sh.getRange(2,1,sh.getLastRow()-1,5).clearContent();
  order.forEach(function (k, i) { sh.getRange(i+2,1,1,5).setValues([[k, m[k].res, m[k].eq, m[k].c, m[k].v]]); });
}

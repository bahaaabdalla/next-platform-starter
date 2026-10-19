// Regenerate panel_html.js after editing panel.html:  node lib/build_panel.mjs
import fs from "node:fs";
const h=fs.readFileSync(new URL("./panel.html",import.meta.url),"utf8");
fs.writeFileSync(new URL("./panel_html.js",import.meta.url),"// generated from panel.html by build_panel.mjs — do not edit\nexport const PANEL_HTML="+JSON.stringify(h)+";\n");

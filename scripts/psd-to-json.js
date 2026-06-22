const PSD = require("psd");
const fs = require("fs");

const file = process.argv[2];

if (!file) {
  console.error("Usage: node psd-to-json.js file.psd");
  process.exit(1);
}

const psd = PSD.fromFile(file);
psd.parse();

const layers = [];

psd.tree().descendants().forEach(layer => {
  if (layer.isGroup()) return;

  const text = layer.text && layer.text.value;

  if (text) {
    layers.push({
      id: layer.name.toLowerCase().replace(/\s+/g, "_"),
      label: layer.name,
      x: layer.left,
      y: layer.top,
      fontSize: 16,
      value: ""
    });
  }
});

const output = {
  width: psd.image.width,
  height: psd.image.height,
  fields: layers
};

const outFile = file.replace(".psd", ".json");
fs.writeFileSync(outFile, JSON.stringify(output, null, 2));

console.log("Converted:", outFile);

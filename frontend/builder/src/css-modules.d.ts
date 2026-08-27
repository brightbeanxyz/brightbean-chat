// TypeScript 7 refuses a side-effect import of a file it has no declaration
// for, so `import "@xyflow/react/dist/style.css"` in main.tsx became
// TS2882. The bundler resolves those imports; the type checker only needs to
// be told they exist.
declare module "*.css";

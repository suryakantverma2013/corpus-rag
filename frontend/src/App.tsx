/**
 * Corpus GUI root. Intentionally bare for the T-004 scaffold — it only mounts
 * the token stylesheet (imported in main.tsx) so `vite build` is green.
 * The three-column shell (sidebar / chat / stats) lands in T-502; runtime
 * theming (data-theme toggle, accent override) lands in T-501.
 */
function App() {
  return <div className="app-root" />;
}

export default App;

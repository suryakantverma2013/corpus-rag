/**
 * Corpus GUI root. Owns the FR-SYS-04 configurable-prop boundary and mounts the theme
 * runtime (§4.8). The three-column shell lands inside the provider in T-502.
 */
import { ThemeProvider } from './theme/ThemeProvider';

export interface AppProps {
  /**
   * FR-SYS-04 / FR-THM-03 — overrides `--accent` in both themes.
   *
   * Intentionally has NO default value here. FR-SYS-04's default `#7C86F8` is carried by the
   * `--accent` token in `src/styles/tokens.css`, per theme; defaulting it in JS would write
   * the dark accent onto the light theme and destroy the `#5B66E8` NFR-VIS-02 specifies for
   * it. See R-58(2).
   */
  accent?: string;
  // brandName / showStats (FR-SYS-04, FR-LAY-02) join this interface in T-502.
}

function App({ accent }: AppProps) {
  return (
    <ThemeProvider accent={accent}>
      <div className="app-root" />
    </ThemeProvider>
  );
}

export default App;

import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import { LocalSessionGate } from './LocalSessionGate';
import { initializeLocalSession, protectLocalSessionRequests } from './api/localSession';
import { ThemeProvider } from './theme/ThemeProvider';
import './styles/index.css';

protectLocalSessionRequests();
const session = initializeLocalSession(window.location, window.history);
// React subscribes during mounting. Mark early network rejections handled.
void session.catch(() => {});
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <LocalSessionGate initial={session}><BrowserRouter><App /></BrowserRouter></LocalSessionGate>
    </ThemeProvider>
  </StrictMode>,
);

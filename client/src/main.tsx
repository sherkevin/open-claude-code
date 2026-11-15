import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';
import './workbench-theme.css';
import './enterprise-theme.css';
import './auth.css';
import './ai-detail.css';
// Keep route styles available even when a later JS chunk needs a network retry.
import './source-evidence.css';
import './business-result.css';
import './connections-onboarding.css';
import './capability-settings.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);

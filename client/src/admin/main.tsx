import React from 'react';
import ReactDOM from 'react-dom/client';
import AdminApp from './AdminApp';
import '../styles.css';
import './admin.css';

import IconPalette from '../IconPalette';
import '../flint-icons.css';

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><IconPalette/><AdminApp/></React.StrictMode>);

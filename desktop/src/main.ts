import './styles.css';
import { mount } from 'svelte';

import App from './App.svelte';
import { installDesktopBridge } from './lib/desktop';

installDesktopBridge();

mount(App, { target: document.getElementById('app')! });

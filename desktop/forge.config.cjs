const path = require('node:path');

module.exports = {
  packagerConfig: {
    asar: true,
    appBundleId: 'chat.rati.swarm',
    appCategoryType: 'public.app-category.finance',
    executableName: process.platform === 'darwin' ? 'RATi Swarm' : 'rati-swarm',
    extraResource: [
      path.join(__dirname, 'resources', 'scanner'),
      path.join(__dirname, '..', 'LICENSE'),
    ],
    ignore: [
      /^\/node_modules(?:\/|$)/,
      /^\/out(?:\/|$)/,
      /^\/resources(?:\/|$)/,
      /^\/\.scanner-(?:build|dist|spec)(?:\/|$)/,
    ],
    name: 'RATi Swarm',
  },
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: { name: 'rati_swarm', setupExe: 'RATi-Swarm-Setup.exe' },
    },
    { name: '@electron-forge/maker-zip', platforms: ['darwin'] },
    {
      name: '@electron-forge/maker-deb',
      config: { options: { bin: 'rati-swarm' } },
    },
  ],
};

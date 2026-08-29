const path = require('node:path');

module.exports = {
  packagerConfig: {
    asar: true,
    appBundleId: 'chat.rati.runners',
    appCategoryType: 'public.app-category.finance',
    executableName: process.platform === 'darwin' ? 'RATi Runners' : 'rati-runners',
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
    name: 'RATi Runners',
  },
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: { name: 'rati_runners', setupExe: 'RATi-Runners-Setup.exe' },
    },
    { name: '@electron-forge/maker-zip', platforms: ['darwin'] },
    { name: '@electron-forge/maker-dmg', config: { format: 'ULFO' } },
    {
      name: '@electron-forge/maker-deb',
      config: { options: { bin: 'rati-runners' } },
    },
  ],
};

const path = require('node:path');

module.exports = {
  packagerConfig: {
    asar: true,
    appBundleId: 'chat.rati.desktop',
    appCategoryType: 'public.app-category.finance',
    executableName: 'RATi',
    extraResource: [path.join(__dirname, 'resources', 'scanner')],
    ignore: [
      /^\/node_modules(?:\/|$)/,
      /^\/out(?:\/|$)/,
      /^\/resources(?:\/|$)/,
      /^\/\.scanner-(?:build|dist|spec)(?:\/|$)/,
    ],
    name: 'RATi',
  },
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: { name: 'rati_desktop', setupExe: 'RATi-Setup.exe' },
    },
    { name: '@electron-forge/maker-zip', platforms: ['darwin'] },
    { name: '@electron-forge/maker-dmg', config: { format: 'ULFO' } },
    { name: '@electron-forge/maker-deb', config: {} },
  ],
};

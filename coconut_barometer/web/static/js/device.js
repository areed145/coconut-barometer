// What the device is, where that changes what the page does. Imported by modules the tests load in Node too, so
// nothing here may need a browser to load.

// iPhone or iPad. Since iPadOS 13 an iPad's Safari says it's a Mac, so a Mac with a touch screen is one too.
const nav = globalThis.navigator;
export const IOS = /iPhone|iPad|iPod/.test(nav?.userAgent || "") || (nav?.platform === "MacIntel" && nav?.maxTouchPoints > 1);

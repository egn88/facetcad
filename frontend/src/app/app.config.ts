import {
  ApplicationConfig,
  provideBrowserGlobalErrorListeners,
  provideZonelessChangeDetection,
} from '@angular/core';
import { provideHttpClient, withFetch } from '@angular/common/http';

/**
 * Zoneless change detection is deliberate: the viewport runs its own
 * requestAnimationFrame loop over a three.js scene, and Zone.js would otherwise
 * schedule a change-detection pass on every single frame. With signals driving
 * the UI, Angular only re-renders when state actually changes.
 */
export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideZonelessChangeDetection(),
    provideHttpClient(withFetch()),
  ],
};

import { registerRootComponent } from 'expo';

// Defines the background check-in task: it must exist when the bundle loads.
import './src/checkin';
import App from './App';

registerRootComponent(App);

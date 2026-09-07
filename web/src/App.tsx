import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import { AppProvider, makeQueryClient } from '@/design-system/providers/AppProvider';
import { AppRoutes } from './routes';

export { makeQueryClient };

const router = createBrowserRouter([{
  path: '*',
  element: <AppProvider><AppRoutes /></AppProvider>,
}]);

export function App(): JSX.Element {
  return <RouterProvider router={router} />;
}

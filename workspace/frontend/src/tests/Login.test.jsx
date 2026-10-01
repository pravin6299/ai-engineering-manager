import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { BrowserRouter } from 'react-router-dom';
import Login from '../pages/Login.jsx';

vi.mock('../services/auth.js', () => {
  return {
    login: vi.fn(() => Promise.resolve({ access_token: 'abc', refresh_token: 'def', token_type: 'bearer' }))
  };
});

const renderWithRouter = (ui) => {
  return render(<BrowserRouter>{ui}</BrowserRouter>);
};

describe('Login Page', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  test('renders form fields and register link', () => {
    renderWithRouter(<Login />);
    expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /register here/i })).toBeInTheDocument();
  });

  test('shows validation errors when fields are empty', async () => {
    renderWithRouter(<Login />);
    fireEvent.click(screen.getByRole('button', { name: /login/i }));
    expect(await screen.findByText(/email is required/i)).toBeInTheDocument();
    expect(await screen.findByText(/password is required/i)).toBeInTheDocument();
  });

  test('calls login service and stores token on success', async () => {
    const { login } = await import('../services/auth.js');
    renderWithRouter(<Login />);
    fireEvent.change(screen.getByLabelText(/email/i), { target: { value: 'user@example.com' } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: 'secret' } });
    fireEvent.click(screen.getByRole('button', { name: /login/i }));

    await waitFor(() => expect(login).toHaveBeenCalledWith({ email: 'user@example.com', password: 'secret' }));
    expect(localStorage.getItem('access_token')).toBe('abc');
    expect(localStorage.getItem('refresh_token')).toBe('def');
  });
});

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { BrowserRouter } from 'react-router-dom';
import Register from '../pages/Register.jsx';

vi.mock('../services/auth.js', () => {
  return {
    register: vi.fn(() => Promise.resolve({ id: 1, email: 'new@example.com', role: 'student' }))
  };
});

const renderWithRouter = (ui) => {
  return render(<BrowserRouter>{ui}</BrowserRouter>);
};

describe('Register Page', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
  });

  test('renders registration form fields and login link', () => {
    renderWithRouter(<Register />);
    expect(screen.getByLabelText(/^email:/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/role/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^password:/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /login here/i })).toBeInTheDocument();
  });

  test('shows validation errors when fields are empty or invalid', async () => {
    renderWithRouter(<Register />);
    fireEvent.click(screen.getByRole('button', { name: /register/i }));
    expect(await screen.findByText(/email is required/i)).toBeInTheDocument();
    expect(await screen.findByText(/password is required/i)).toBeInTheDocument();
  });

  test('shows error when passwords do not match', async () => {
    renderWithRouter(<Register />);
    fireEvent.change(screen.getByLabelText(/^email:/i), { target: { value: 'user@example.com' } });
    fireEvent.change(screen.getByLabelText(/^password:/i), { target: { value: 'password123' } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: 'password999' } });
    fireEvent.click(screen.getByRole('button', { name: /register/i }));

    expect(await screen.findByText(/passwords do not match/i)).toBeInTheDocument();
  });

  test('calls register service on successful submission', async () => {
    const { register } = await import('../services/auth.js');
    renderWithRouter(<Register />);

    fireEvent.change(screen.getByLabelText(/^email:/i), { target: { value: 'newuser@example.com' } });
    fireEvent.change(screen.getByLabelText(/role/i), { target: { value: 'student' } });
    fireEvent.change(screen.getByLabelText(/^password:/i), { target: { value: 'password123' } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: 'password123' } });
    
    fireEvent.click(screen.getByRole('button', { name: /register/i }));

    await waitFor(() => {
      expect(register).toHaveBeenCalledWith({
        email: 'newuser@example.com',
        password: 'password123',
        role: 'student'
      });
    });
  });
});

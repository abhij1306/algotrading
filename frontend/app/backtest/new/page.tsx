import { useEffect } from 'react';
import { useNavigate } from 'react-router';

export default function BacktestNewRedirectPage() {
  const navigate = useNavigate();

  useEffect(() => {
    navigate('/backtest', { replace: true });
  }, [navigate]);

  return null;
}

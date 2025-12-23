/**
 * SmartTrader Prism Logo - 3D Rotating Tesseract
 * Pure CSS animation, no external dependencies
 */

export default function SmartTraderLogo({ className = "" }: { className?: string }) {
    return (
        <div className={`relative ${className}`} style={{ width: '40px', height: '40px' }}>
            <div className="prism-container">
                {/* Outer Frame */}
                <div className="prism-outer">
                    {/* Front Face */}
                    <div className="prism-face prism-front" />
                    {/* Back Face */}
                    <div className="prism-face prism-back" />
                    {/* Left Face */}
                    <div className="prism-face prism-left" />
                    {/* Right Face */}
                    <div className="prism-face prism-right" />
                    {/* Top Face */}
                    <div className="prism-face prism-top" />
                    {/* Bottom Face */}
                    <div className="prism-face prism-bottom" />
                </div>

                {/* Inner Core (glowing center) */}
                <div className="prism-core" />
            </div>

            <style jsx>{`
        .prism-container {
          width: 100%;
          height: 100%;
          position: relative;
          transform-style: preserve-3d;
          animation: rotatePrism 12s linear infinite;
        }

        @keyframes rotatePrism {
          0% {
            transform: rotateY(0deg) rotateX(0deg) rotateZ(0deg);
          }
          100% {
            transform: rotateY(360deg) rotateX(360deg) rotateZ(180deg);
          }
        }

        .prism-outer {
          position: absolute;
          width: 100%;
          height: 100%;
          transform-style: preserve-3d;
        }

        .prism-face {
          position: absolute;
          width: 100%;
          height: 100%;
          background: rgba(255, 255, 255, 0.05);
          border: 1px solid #06B6D4;
          box-shadow: 0 0 10px rgba(6, 182, 212, 0.4), inset 0 0 10px rgba(6, 182, 212, 0.2);
          backdrop-filter: blur(2px);
        }

        .prism-front {
          transform: translateZ(20px);
        }

        .prism-back {
          transform: translateZ(-20px) rotateY(180deg);
        }

        .prism-left {
          transform: rotateY(-90deg) translateZ(20px);
        }

        .prism-right {
          transform: rotateY(90deg) translateZ(20px);
        }

        .prism-top {
          transform: rotateX(90deg) translateZ(20px);
        }

        .prism-bottom {
          transform: rotateX(-90deg) translateZ(20px);
        }

        .prism-core {
          position: absolute;
          width: 12px;
          height: 12px;
          top: 50%;
          left: 50%;
          transform: translate(-50%, -50%);
          background: radial-gradient(circle, #06B6D4 0%, transparent 70%);
          border-radius: 50%;
          box-shadow: 0 0 15px #06B6D4, 0 0 30px #06B6D4;
          animation: pulse 2s ease-in-out infinite;
        }

        @keyframes pulse {
          0%, 100% {
            opacity: 1;
            transform: translate(-50%, -50%) scale(1);
          }
          50% {
            opacity: 0.6;
            transform: translate(-50%, -50%) scale(1.2);
          }
        }
      `}</style>
        </div>
    );
}

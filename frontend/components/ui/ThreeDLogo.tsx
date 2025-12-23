"use client";

import React from 'react';

export const ThreeDLogo = () => {
    return (
        <div className="relative w-8 h-8 group perspective-[1000px]">
            <div className="w-full h-full relative transform-style-3d animate-slow-spin-3d">
                {/* Front */}
                <div className="absolute inset-0 bg-gradient-to-br from-cyan-500 to-blue-600 rounded-lg flex items-center justify-center translate-z-4 shadow-[0_0_15px_rgba(6,182,212,0.5)] border border-cyan-400/30 opacity-90 backface-hidden">
                    <span className="font-black text-white text-[10px] tracking-tighter">ST</span>
                </div>
                {/* Back */}
                <div className="absolute inset-0 bg-gradient-to-br from-purple-600 to-pink-600 rounded-lg flex items-center justify-center -translate-z-4 rotate-y-180 shadow-[0_0_15px_rgba(147,51,234,0.5)] border border-purple-400/30 opacity-90 backface-hidden">
                    <span className="font-black text-white text-[10px] tracking-tighter">AI</span>
                </div>
                {/* Right */}
                <div className="absolute inset-0 bg-gradient-to-br from-blue-600 to-cyan-500 rounded-lg flex items-center justify-center rotate-y-90 translate-x-4 opacity-80 border border-blue-400/20">
                    <div className="w-1 h-4 bg-white/20 rounded-full"></div>
                </div>
                {/* Left */}
                <div className="absolute inset-0 bg-gradient-to-br from-cyan-500 to-blue-600 rounded-lg flex items-center justify-center -rotate-y-90 -translate-x-4 opacity-80 border border-blue-400/20">
                    <div className="w-1 h-4 bg-white/20 rounded-full"></div>
                </div>
            </div>

            <style jsx>{`
                .perspective-\\[1000px\\] {
                    perspective: 1000px;
                }
                .transform-style-3d {
                    transform-style: preserve-3d;
                }
                .translate-z-4 {
                    transform: translateZ(16px);
                }
                .-translate-z-4 {
                    transform: translateZ(-16px);
                }
                .rotate-y-180 {
                    transform: rotateY(180deg) translateZ(16px);
                }
                .rotate-y-90 {
                    transform: rotateY(90deg) translateZ(16px);
                }
                .-rotate-y-90 {
                    transform: rotateY(-90deg) translateZ(16px);
                }
                .backface-hidden {
                    backface-visibility: hidden;
                }
                .animate-slow-spin-3d {
                    animation: spin3d 8s infinite linear;
                }
                .group:hover .animate-slow-spin-3d {
                    animation-duration: 4s; /* Speed up on hover */
                }
                    
                @keyframes spin3d {
                    0% { transform: rotateY(0deg) rotateX(0deg); }
                    100% { transform: rotateY(360deg) rotateX(0deg); } /* Just rotate Y for clean spin */
                }
            `}</style>
        </div>
    );
};

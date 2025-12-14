import React from 'react';

interface CircularProgressProps {
    value: number; // 0-10 risk score
    grade: string;
    size?: number;
}

export default function CircularProgress({ value, grade, size = 160 }: CircularProgressProps) {
    // Convert 0-10 scale to percentage (inverted - lower is better)
    const percentage = ((10 - value) / 10) * 100;
    const radius = (size - 24) / 2;
    const circumference = 2 * Math.PI * radius;
    const strokeDashoffset = circumference - (percentage / 100) * circumference;

    // Color based on grade
    const getColor = () => {
        if (grade.startsWith('A')) return '#10b981'; // green-500
        if (grade.startsWith('B')) return '#3b82f6'; // blue-500
        if (grade.startsWith('C')) return '#eab308'; // yellow-500
        if (grade.startsWith('D')) return '#f97316'; // orange-500
        return '#ef4444'; // red-500
    };

    return (
        <div className="circular-progress" style={{ width: size, height: size }}>
            <svg width={size} height={size}>
                <circle
                    className="circular-progress-bg"
                    cx={size / 2}
                    cy={size / 2}
                    r={radius}
                />
                <circle
                    className="circular-progress-fill"
                    cx={size / 2}
                    cy={size / 2}
                    r={radius}
                    stroke={getColor()}
                    strokeDasharray={circumference}
                    strokeDashoffset={strokeDashoffset}
                />
            </svg>
            <div className="circular-progress-text">
                <div className="text-5xl font-bold" style={{ color: getColor() }}>
                    {grade}
                </div>
                <div className="text-sm opacity-60 mt-1">Risk Grade</div>
                <div className="text-xs opacity-50 mt-1">Score: {value.toFixed(1)}/10</div>
            </div>
        </div>
    );
}

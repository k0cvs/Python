def calculate_j_pole_dimensions(frequency):
    # Speed of light in meters per second
    c = 299792458
    
    # Convert frequency to Hz
    frequency_hz = frequency * 1e6
    
    # Calculate wavelength
    wavelength = c / frequency_hz
    
    # Calculate dimensions in meters
    quarter_wavelength_m = wavelength / 4
    three_quarter_wavelength_m = 3 * quarter_wavelength_m
    
    # Calculate spacing between the two elements (typically 0.025 wavelength) in meters
    spacing_m = 0.025 * wavelength
    
    # Convert dimensions to centimeters (1 meter = 100 centimeters)
    quarter_wavelength_cm = quarter_wavelength_m * 100
    three_quarter_wavelength_cm = three_quarter_wavelength_m * 100
    spacing_cm = spacing_m * 100
    
    return quarter_wavelength_cm, three_quarter_wavelength_cm, spacing_cm

# Example usage
center_frequency = float(raw_input("Enter the center frequency in MHz: "))
quarter_wavelength, three_quarter_wavelength, spacing = calculate_j_pole_dimensions(center_frequency)

print "Quarter Wavelength: {} centimeters".format(quarter_wavelength)
print "Three Quarter Wavelength: {} centimeters".format(three_quarter_wavelength)
print "Required Spacing between the two elements: {} centimeters".format(spacing)
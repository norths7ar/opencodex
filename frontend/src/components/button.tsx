import { forwardRef, type ButtonHTMLAttributes } from "react";
import { buttonStyles, type ButtonVariant, type ButtonSize } from "./button-styles";
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "outline", size = "md", className, type = "button", ...props },
  ref,
) {
  return (
    <button ref={ref} type={type} className={buttonStyles(variant, size, className)} {...props} />
  );
});
